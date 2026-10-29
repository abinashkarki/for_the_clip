"""Local scheduler and metrics sync: one background thread, durable in SQLite, single-flight.

Each pass (1) attaches finished exports to posts waiting for a render, (2) uploads posts that are due, with
exponential backoff and a resumable upload session saved on the post, (3) polls platform status and metrics on
the decaying schedule 1 h, 6 h, 24 h, 72 h, 7 d, 28 d after publication. All state is in `posts`, so a restart resumes.
"""
import logging
import fcntl
from pathlib import Path
import threading
import time

from . import runtime
from .base import AuthError, POLL_OFFSETS, PublishError

LOG = logging.getLogger('clippie.publish')
MAX_ATTEMPTS = 6
UPLOAD_BACKOFF = (60, 300, 900, 1800, 3600)
POLL_BACKOFF = 900.0
TICK_SECONDS = 30.0
SCHEDULE_CHECK_SLACK = 120.0  # look at a scheduled post this long after it should have gone live


def upload_delay(attempts):
    return UPLOAD_BACKOFF[min(attempts - 1, len(UPLOAD_BACKOFF) - 1)]


def next_stage_time(base, stage, now):
    """(stage, time) of the first poll after `stage` that is still in the future; (stage, None) when finished."""
    stage += 1
    while stage < len(POLL_OFFSETS) and base + POLL_OFFSETS[stage] <= now:
        stage += 1
    return (stage, base + POLL_OFFSETS[stage]) if stage < len(POLL_OFFSETS) else (stage, None)


class SyncService:
    def __init__(self, root, now=time.time):
        self.root, self.now = Path(root), now
        self.flight = threading.Lock()
        self.wake, self.stop, self.thread = threading.Event(), threading.Event(), None

    @property
    def db(self):
        return runtime.store(self.root)

    def studio(self):
        from .. import studio_api
        return studio_api.studio()

    # -- one pass -----------------------------------------------------------
    def run_once(self):
        """Run one pass unless one is already running. Returns counts, or None when skipped."""
        if not self.flight.acquire(blocking=False):
            return None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            lock = (self.root / 'publish-sync.lock').open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                return None
            try:
                self.db.recover()
                return self._run_locked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
        finally:
            self.flight.release()

    def _run_locked(self):
        http = runtime.http_factory()
        try:
            return {'renders': self.attach_renders(), 'uploads': self.upload_due(http), 'polls': self.poll_due(http)}
        finally:
            http.close()

    def attach_renders(self):
        done = 0
        for post in self.db.awaiting_render():
            try:
                job = self.studio().get_job(post['render_job_id'])
            except KeyError:
                self.db.update_post(post['id'], status='failed', error='The export for this clip is missing. Publish again.')
                continue
            if job['status'] == 'failed':
                self.db.update_post(post['id'], status='failed', error=f'The export failed: {job.get("error") or "unknown error"}')
            elif job['status'] == 'done':
                artifact = job['result']['video_url'].rstrip('/').split('/')[-2]
                self.db.update_post(post['id'], artifact_id=artifact, render_job_id=None)
                done += 1
        return done

    def upload_due(self, http):
        count = 0
        for post in self.db.due_uploads(self.now()):
            self.upload_one(http, post)
            count += 1
        return count

    def upload_one(self, http, post):
        from .. import final_quality
        db, post_id = self.db, post['id']
        try:
            export = self.studio().get_export(post['artifact_id'])
            path = export['video_path']
            channel = db.get_channel(post['channel_id'])
        except KeyError:
            return db.update_post(post_id, status='failed', error='The export or channel for this post no longer exists.')
        try:
            final_quality.verify_artifact(export, short_id=post['short_id'], revision_id=post['revision_id'])
        except final_quality.FinalQualityError as error:
            return self.upload_failed(post_id, PublishError(str(error), retryable=False))
        db.update_post(post_id, status='uploading', error=None)
        fresh = db.get_post(post_id)
        try:
            publisher = runtime.publisher(http, db, channel, save_session=lambda s: db.update_post(post_id, upload_session=s))
            handle = publisher.upload(fresh, path)
            if handle.get('video_id'):
                # Persist the remote identity before a separate status request can fail or the app can stop.
                db.update_post(post_id, external_id=handle['video_id'], upload_session=None, next_poll_at=self.now())
            result = publisher.schedule(db.get_post(post_id), handle)
        except (OSError, PublishError) as error:
            return self.upload_failed(post_id, error)
        self.record_result(db.get_post(post_id), result)

    def upload_failed(self, post_id, error):
        post = self.db.get_post(post_id)
        attempts = post['attempts'] + 1
        text = str(error) if isinstance(error, PublishError) else f'Could not read the exported video: {error}'
        retry = isinstance(error, PublishError) and error.retryable and attempts < MAX_ATTEMPTS
        fields = {'attempts': attempts, 'error': text}
        if post.get('external_id'):
            return self.db.update_post(post_id, status='scheduled', next_poll_at=self.now() + POLL_BACKOFF, **fields)
        if getattr(error, 'clear_session', False):
            fields['upload_session'] = None
        if retry:
            self.db.update_post(post_id, status='scheduled', next_attempt_at=self.now() + upload_delay(attempts), **fields)
        else:
            self.db.update_post(post_id, status='failed', next_attempt_at=None, **fields)
        LOG.warning('publish upload failed for %s: %s', post_id, text)

    def record_result(self, post, result):
        fields = {'external_id': result.external_id, 'url': result.url or post['url'], 'upload_session': None,
                  'error': None, 'attempts': 0, 'poll_stage': 0, 'status': result.status}
        if result.status == 'published':
            published = result.published_at or self.now()
            fields.update(published_at=published, next_poll_at=published + POLL_OFFSETS[0])
        else:
            when = post['scheduled_at'] or self.now()
            fields.update(next_poll_at=max(when, self.now()) + SCHEDULE_CHECK_SLACK)
        self.db.update_post(post['id'], **fields)

    def poll_due(self, http):
        count = 0
        for post in self.db.due_polls(self.now()):
            self.poll_one(http, post)
            count += 1
        return count

    def poll_one(self, http, post):
        db = self.db
        try:
            channel = db.get_channel(post['channel_id'])
            publisher = runtime.publisher(http, db, channel)
            if post['status'] == 'scheduled':
                return self.check_scheduled(post, publisher)
            fetched = publisher.metrics(post)
        except KeyError:
            return db.update_post(post['id'], next_poll_at=None, error='The channel for this post was disconnected.')
        except PublishError as error:
            attempts = post['attempts'] + 1
            delay = min(POLL_BACKOFF * 2 ** (attempts - 1), 6 * 3600.0)
            return db.update_post(post['id'], attempts=attempts, error=f'Metrics: {error}', next_poll_at=self.now() + delay)
        now = self.now()
        db.add_snapshot(post['id'], fetched.metrics, now)
        if fetched.retention:
            db.add_retention(post['id'], fetched.retention, now)
        stage, when = next_stage_time(post['published_at'] or post['created_at'], post['poll_stage'], now)
        db.update_post(post['id'], poll_stage=stage, next_poll_at=when, attempts=0, error=None)

    def check_scheduled(self, post, publisher):
        found = publisher.status(post)
        now = self.now()
        if found is None:
            return self.db.update_post(post['id'], status='failed', next_poll_at=None,
                                       error='The platform has no record of this video. It may have been deleted there.')
        if found.status == 'published':
            published = found.published_at or now
            return self.db.update_post(post['id'], status='published', published_at=published, url=found.url or post['url'],
                                       poll_stage=0, attempts=0, error=None,
                                       next_poll_at=max(published + POLL_OFFSETS[0], now))
        wait = max((post['scheduled_at'] or now) - now, 0) + SCHEDULE_CHECK_SLACK
        self.db.update_post(post['id'], next_poll_at=now + max(wait, 300.0), attempts=0, error=None)

    # -- thread -------------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self.loop, name='clippie-publish', daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            try:
                if not self.root.exists():
                    return
                self.run_once()
            except Exception:  # the scheduler must never die; the next pass retries
                LOG.exception('publish sync pass failed')
            self.wake.wait(TICK_SECONDS)
            self.wake.clear()

    def shutdown(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)


_SERVICES, _GUARD = {}, threading.Lock()


def service(root):
    with _GUARD:
        key = str(root)
        if key not in _SERVICES:
            _SERVICES[key] = SyncService(Path(root))
        return _SERVICES[key]


def ensure_started(root):
    service(root).start()


def start_background(root):
    """App lifespan hook: start the scheduler only when publishing has been used on this data directory."""
    from pathlib import Path
    if (Path(root) / 'publish.sqlite3').exists():
        ensure_started(root)


def kick(root):
    service(root).wake.set()


def stop_all():
    with _GUARD:
        services = list(_SERVICES.values())
        _SERVICES.clear()
    for item in services:
        item.shutdown()
