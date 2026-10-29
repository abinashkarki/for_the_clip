"""Video lifecycle: idle clocks, freeing source media, trash and purge, shared-source refcounts, master GC and the sweeper.

Layers (see docs/VIDEO_LIFECYCLE_PLAN_2026-10-28.md): source media is freed after an idle period; working files go
with it; exported clips and the project record stay until the project is deleted.

Metadata lives in two small tables inside `studio.sqlite3` (`project_lifecycle`, `lifecycle_settings`), created here.
That keeps collection documents (fenced by task leases) and `project_options` (returned verbatim by the API) untouched.
Per-source facts (`source_freed_at`, `source_sha256`, `source_size`, `freed_provider_id`) are stored on the job document.

Nothing here deletes a path outside the data root, and a path is only deleted when it is inside a Clippie-owned folder:
`<root>/<job_id>/`, `<root>/uploads/<id>/`, `<root>/final-sources/`, and the cache folders named below.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger('clippie.lifecycle')
DEFAULT_RETENTION_DAYS = 15
RETENTION_CHOICES = (0, 7, 15, 30)
TRASH_DAYS = 7
DISMISS_DAYS = 7
SWEEP_SECONDS = 6 * 3600
PENDING_POST = {'scheduled', 'uploading', 'rendering'}
KEEP_SUFFIXES = {'.json', '.txt', '.srt', '.vtt', '.md'}
BUSY_SOURCE = {'queued', 'ingesting', 'transcribing', 'selecting', 'exporting', 'restoring'}
FREED_MESSAGE = "This project's source was freed. Restore it to edit or export."
TRASHED_MESSAGE = 'This project is in the trash. Restore it to edit or export.'
UPLOAD_MESSAGE = 'Drop the original file to restore it'
DESTRUCTIVE_LIFECYCLE_ENABLED = False  # Only isolated algorithm tests may opt in; coordination is incomplete.
DELETION_PAUSED = 'Source cleanup and project deletion are temporarily unavailable while Clippie protects active work.'
COUNTERS = {'sweeps': 0, 'freed_sources': 0, 'purged_projects': 0, 'dismissed_sources': 0, 'removed_masters': 0, 'errors': 0}


class LifecycleError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# -- context ---------------------------------------------------------------------------

class Ctx:
    """Everything the lifecycle needs, injectable for tests."""

    def __init__(self, root, collections, jobs, studio, publish=None, retry=None, clock=time.time, project_name=None):
        self.root = Path(root).resolve()
        self.collections, self.jobs, self.studio = collections, jobs, studio
        self.publish = publish or (lambda: None)   # callable returning a PublishStore or None
        self.retry = retry                          # callable(source_id) re-running a failed source
        self.clock = clock
        self.project_name = project_name or self._default_name
        with self.studio._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS project_lifecycle (id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS lifecycle_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def _default_name(self, collection):
        if collection.get('name'):
            return collection['name']
        for source_id in collection['source_ids'][:1]:
            try:
                job = self.jobs.get(source_id)
                return job.get('studio_title') or job.get('title') or 'Untitled project'
            except KeyError:
                pass
        return 'Untitled project'

    # metadata
    def meta_all(self):
        with self.studio._db() as db:
            return {r[0]: json.loads(r[1]) for r in db.execute('SELECT id, document FROM project_lifecycle')}

    def meta(self, project_id):
        with self.studio._db() as db:
            row = db.execute('SELECT document FROM project_lifecycle WHERE id=?', (project_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_meta(self, project_id, **fields):
        with self.studio.lock, self.studio._db() as db:
            row = db.execute('SELECT document FROM project_lifecycle WHERE id=?', (project_id,)).fetchone()
            document = json.loads(row[0]) if row else {}
            document.update(fields)
            db.execute('INSERT INTO project_lifecycle VALUES (?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document',
                       (project_id, json.dumps(document)))
        return document

    def delete_meta(self, project_id):
        with self.studio._db() as db:
            db.execute('DELETE FROM project_lifecycle WHERE id=?', (project_id,))

    # settings
    def retention_days(self):
        with self.studio._db() as db:
            row = db.execute("SELECT value FROM lifecycle_settings WHERE key='source_retention_days'").fetchone()
        try:
            value = int(row[0]) if row else DEFAULT_RETENTION_DAYS
        except ValueError:
            value = DEFAULT_RETENTION_DAYS
        return value if value in RETENTION_CHOICES else DEFAULT_RETENTION_DAYS

    def rollout_at(self):
        """When automatic cleanup first ran on this data folder. Every idle/dismiss/orphan clock starts no
        earlier than this, so upgrading never deletes existing media on first launch."""
        if getattr(self, '_rollout', None) is not None:
            return self._rollout
        with self.studio._db() as db:
            row = db.execute("SELECT value FROM lifecycle_settings WHERE key='lifecycle_started_at'").fetchone()
            if row is None:
                db.execute("INSERT OR IGNORE INTO lifecycle_settings VALUES ('lifecycle_started_at', ?)", (str(self.clock()),))
                row = db.execute("SELECT value FROM lifecycle_settings WHERE key='lifecycle_started_at'").fetchone()
        try:
            self._rollout = float(row[0])
        except (TypeError, ValueError):
            self._rollout = self.clock()
        return self._rollout

    def set_retention_days(self, days):
        if days not in RETENTION_CHOICES or isinstance(days, bool):
            raise LifecycleError(422, 'Choose 7, 15 or 30 days, or 0 to never free sources.')
        with self.studio._db() as db:
            db.execute("INSERT INTO lifecycle_settings VALUES ('source_retention_days', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(days),))
        return days

    def publish_store(self):
        try:
            return self.publish()
        except Exception as error:
            raise LifecycleError(503, 'Publishing state is unavailable. Try again before removing media or projects.') from error


# -- filesystem helpers ----------------------------------------------------------------

def iso(stamp):
    return datetime.fromtimestamp(float(stamp), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return None


def tree_bytes(path):
    """Sum of file sizes under a path, never following links."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        return 0
    if not os.path.isdir(path) or path.is_symlink():
        return info.st_size if not path.is_symlink() else 0
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                item = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            if not os.path.islink(os.path.join(folder, name)):
                total += item.st_size
    return total


def inside(root, path):
    """True when the path's directory resolves under root (the final component is never followed)."""
    try:
        parent = Path(os.path.realpath(Path(path).parent))
        return parent == root or root in parent.parents
    except OSError:
        return False


def remove(ctx, path):
    """Delete a file, link or folder, only when it is inside the data root. Returns bytes freed."""
    path = Path(path)
    if not inside(ctx.root, path) or path == ctx.root:
        return 0
    size = tree_bytes(path)
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        return 0
    return size


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def is_link_source(job):
    return str(job.get('source') or '').startswith(('http://', 'https://'))


def upload_dir(ctx, job):
    """The studio upload folder holding this source's original, when there is one."""
    path = job.get('source_path')
    if not path or is_link_source(job):
        return None
    folder = Path(path).parent
    uploads = ctx.root / 'uploads'
    return folder if folder.parent == uploads else None


def managed_media(ctx, job):
    """Files that freeing this source deletes: media in its job folder and its studio upload copy."""
    files = []
    folder = ctx.root / job['id']
    if folder.is_dir() and not folder.is_symlink():
        for current, _dirs, names in os.walk(folder):
            for name in names:
                path = Path(current) / name
                if path.suffix.lower() not in KEEP_SUFFIXES:
                    files.append(path)
    upload = upload_dir(ctx, job)
    if upload and upload.is_dir():
        files.extend(p for p in upload.iterdir())
    return files


def source_is_freed(job):
    """Remain unavailable until restoration explicitly commits all required media."""
    if not job.get('source_freed_at'):
        return False
    if not is_link_source(job):
        return True
    wanted, found = job.get('freed_provider_id'), (job.get('provenance') or {}).get('id')
    source, preview = job.get('source_path'), job.get('preview_path')
    return not (wanted and found and str(wanted) == str(found) and job.get('status') == 'ready'
                and source and Path(source).is_file() and preview and Path(preview).is_file())


def master_dirs(ctx):
    """[(folder, source_id | None)] for every HD master cache folder."""
    base = ctx.root / 'final-sources'
    result = []
    if not base.is_dir():
        return result
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            identity = json.loads((folder / 'master.json').read_text())['identity']
            result.append((folder, identity.get('source_id')))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            result.append((folder, None))
    return result


def source_cache_paths(ctx, source_id):
    """Per-source cache folders and files: analysis, evidence, visuals, thumbnails."""
    paths = [ctx.root / 'analysis-v3' / source_id]
    paths += list((ctx.root / 'studio' / 'thumbnails').glob(f'src-{source_id}*'))
    evidence = ctx.root / 'source-evidence'
    if evidence.is_dir():
        for path in evidence.glob('evidence-*.json'):
            try:
                units = json.loads(path.read_text()).get('units') or []
            except (OSError, ValueError, AttributeError):
                continue
            if any(isinstance(u, dict) and u.get('source_id') == source_id for u in units[:3]):
                paths += [path, path.with_suffix('.lock')]
    visuals = ctx.root / 'source-visuals'
    if visuals.is_dir():
        for path in visuals.glob('visuals-*'):
            if path.suffix == '.lock' or not path.is_dir():
                continue
            try:
                found = json.loads((path / 'manifest.json').read_text()).get('manifest', {}).get('source_id')
            except (OSError, ValueError, AttributeError):
                continue
            if found == source_id:
                paths += [path, path.with_name(path.name + '.lock')]
    return paths


# -- state snapshot --------------------------------------------------------------------

class Snap:
    """One consistent read of projects, sources, posts and studio jobs for a request or a sweep."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.jobs = {job['id']: job for job in ctx.jobs.all()}
        self.collections = ctx.collections.list_collections()
        self.by_id = {c['id']: c for c in self.collections}
        self.users = {}
        for collection in self.collections:
            for source_id in collection['source_ids']:
                self.users.setdefault(source_id, []).append(collection)
        self._meta = ctx.meta_all()
        self._shorts, self._tasks, self._activity = {}, {}, {}
        self._clip_jobs = None
        self._posts = None
        self._masters = None

    # pieces
    def meta(self, project_id):
        return self._meta.get(project_id, {})

    def shorts(self, collection):
        if collection['id'] not in self._shorts:
            self._shorts[collection['id']] = self.ctx.collections.list_shorts(collection['id'])
        return self._shorts[collection['id']]

    def tasks(self, collection):
        if collection['id'] not in self._tasks:
            run = collection.get('current_run')
            self._tasks[collection['id']] = [t for t in self.ctx.collections.list_tasks(collection['id'])
                                             if not t.get('payload', {}).get('run_id') or t['payload']['run_id'] == run]
        return self._tasks[collection['id']]

    def clip_jobs(self):
        if self._clip_jobs is None:
            self._clip_jobs = {}
            for job in self.ctx.studio.list_jobs():
                self._clip_jobs.setdefault(job.get('clip_id'), []).append(job)
        return self._clip_jobs

    def posts(self):
        if self._posts is None:
            store = self.ctx.publish_store()
            self._posts = {}
            if store is not None:
                try:
                    posts = store.posts()
                    for post in posts:
                        self._posts.setdefault(post['short_id'], []).append(post)
                except Exception as error:
                    self._posts = None
                    raise LifecycleError(503, 'Publishing state is unavailable. Try again before removing media or projects.') from error
        return self._posts

    def masters(self):
        if self._masters is None:
            self._masters = master_dirs(self.ctx)
        return self._masters

    # derived
    def source_jobs(self, collection):
        return [self.jobs[s] for s in collection['source_ids'] if s in self.jobs]

    def activity(self, collection):
        """Newest of creation, explicit touches, revisions, runs, exports/reframes and posts (as seconds)."""
        key = collection['id']
        if key in self._activity:
            return self._activity[key]
        stamps = [collection.get('created_at') or 0, self.meta(key).get('touched_at') or 0]
        if collection.get('current_run'):
            try:
                run = self.ctx.collections.get_run(collection['current_run'])
                stamps += [run.get('created_at') or 0, run.get('finished_at') or 0]
            except KeyError:
                pass
        for short in self.shorts(collection):
            for revision in self.ctx.collections.list_revisions(short['id']):
                stamps.append(revision.get('created_at') or 0)
            for job in self.clip_jobs().get(short['id'], []):
                stamps += [job.get('created_at') or 0, job.get('updated_at') or 0]
            for post in self.posts().get(short['id'], []):
                stamps += [post.get('created_at') or 0, post.get('published_at') or 0]
        self._activity[key] = max(float(s) for s in stamps if s is not None)
        return self._activity[key]

    def idle_since(self, collection):
        return max(self.activity(collection), self.ctx.rollout_at())

    def blocked(self, collection):
        """Why this project cannot be trashed, freed or purged right now, or None."""
        # Even a project with no current shorts must not bypass an unavailable
        # publishing store: its records may be incomplete or temporarily unreadable.
        self.posts()
        if any(t['status'] in {'queued', 'active'} for t in self.tasks(collection)):
            return 'This project is still working. Wait for it to finish or cancel it first.'
        for job in self.source_jobs(collection):
            if job['status'] in BUSY_SOURCE:
                return 'A source for this project is still being prepared.'
        for short in self.shorts(collection):
            if any(j.get('status') in {'queued', 'running'} for j in self.clip_jobs().get(short['id'], [])):
                return 'An export or reframe for this project is still running.'
            if any(p['status'] in PENDING_POST for p in self.posts().get(short['id'], [])):
                return 'A post from this project is scheduled or uploading. Cancel it or let it finish first.'
        return None

    def trashed_at(self, collection):
        return collection.get('trashed_at') or self.meta(collection['id']).get('trashed_at')

    def is_trashed(self, collection):
        return bool(self.trashed_at(collection))

    def lifecycle(self, collection):
        if self.is_trashed(collection):
            return 'trashed'
        return 'source_freed' if any(source_is_freed(j) for j in self.source_jobs(collection)) else 'active'

    def source_bytes(self, job):
        masters = sum(tree_bytes(f) for f, sid in self.masters() if sid == job['id'])
        return sum(tree_bytes(p) for p in managed_media(self.ctx, job)) + masters

    def revision_ids(self, collection):
        return [r['id'] for s in self.shorts(collection) for r in self.ctx.collections.list_revisions(s['id'])]

    def project_sizes(self, collection):
        root = self.ctx.root / 'collection-artifacts'
        working = exports = 0
        for revision_id in self.revision_ids(collection):
            folder = root / revision_id
            if folder.is_dir():
                for attempt in folder.iterdir():
                    size = tree_bytes(attempt)
                    if attempt.name.startswith('export-'):
                        exports += size
                    else:
                        working += size
            exports += tree_bytes(root / 'studio' / revision_id)
        thumbs = self.ctx.root / 'studio' / 'thumbnails'
        for short in self.shorts(collection):
            working += sum(tree_bytes(p) for p in thumbs.glob(f'clip-{short["id"]}-*'))
        return working, exports

    def expires_at(self, collection):
        """When the project's source is due to be freed, or None (pinned, never, blocked, trashed or already freed)."""
        if not DESTRUCTIVE_LIFECYCLE_ENABLED:
            return None
        days = self.ctx.retention_days()
        if not days or self.is_trashed(collection) or self.meta(collection['id']).get('keep_source') or self.blocked(collection):
            return None
        due = []
        for job in self.source_jobs(collection):
            if not is_link_source(job) or source_is_freed(job) or not managed_media(self.ctx, job):
                continue
            for user in self.users.get(job['id'], []):
                if self.meta(user['id']).get('keep_source') or self.blocked(user):
                    return None
                due.append(self.idle_since(user) + days * 86400)
        return iso(max(due)) if due else None


def summary_fields(ctx, collection, snap=None):
    """The lifecycle fields ProjectSummary gains."""
    snap = snap or Snap(ctx)
    meta = snap.meta(collection['id'])
    working, exports = snap.project_sizes(collection)
    trashed = snap.trashed_at(collection)
    return {'lifecycle': snap.lifecycle(collection), 'last_activity_at': iso(snap.activity(collection)),
            'keep_source': bool(meta.get('keep_source')), 'source_expires_at': snap.expires_at(collection),
            'size_bytes': {'source': sum(snap.source_bytes(j) for j in snap.source_jobs(collection)),
                           'working': working, 'exports': exports},
            'trashed_at': iso(trashed) if trashed else None,
            'purge_at': iso(trashed + TRASH_DAYS * 86400) if trashed and DESTRUCTIVE_LIFECYCLE_ENABLED else None}


# -- project actions -------------------------------------------------------------------

def touch(ctx, project_id):
    ctx.set_meta(project_id, touched_at=ctx.clock())


def require(ctx, project_id):
    try:
        return ctx.collections.get_collection(project_id)
    except KeyError:
        raise LifecycleError(404, 'Project not found.') from None


def guard_reason(ctx, collection):
    """Message when edits/exports/generation must be refused for this project, else None."""
    if collection.get('trashed_at') or ctx.meta(collection['id']).get('trashed_at'):
        return TRASHED_MESSAGE
    for source_id in collection['source_ids']:
        try:
            if source_is_freed(ctx.jobs.get(source_id)):
                return FREED_MESSAGE
        except KeyError:
            continue
    return None


def trash(ctx, project_id):
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    collection = require(ctx, project_id)
    snap = Snap(ctx)
    if snap.is_trashed(collection):
        return collection
    if reason := snap.blocked(collection):
        raise LifecycleError(409, reason)
    stamp = ctx.clock()
    ctx.set_meta(project_id, trashed_at=stamp)
    return ctx.collections.set_collection_fields(project_id, trashed_at=stamp)


def untrash(ctx, project_id):
    collection = require(ctx, project_id)
    if not (collection.get('trashed_at') or ctx.meta(project_id).get('trashed_at')):
        raise LifecycleError(409, 'This project is not in the trash.')
    ctx.set_meta(project_id, trashed_at=None, touched_at=ctx.clock())
    return ctx.collections.set_collection_fields(project_id, trashed_at=None)


def keep_source(ctx, project_id, keep):
    collection = require(ctx, project_id)
    ctx.set_meta(project_id, keep_source=bool(keep))
    return collection


def free_source_files(ctx, source_id, snap=None):
    """Delete one source's media, masters and regenerable caches; record how to bring it back. Returns bytes freed."""
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    try:
        job = ctx.jobs.get(source_id)
    except KeyError:
        return 0
    if job['status'] in BUSY_SOURCE:
        raise LifecycleError(409, 'This source is still being prepared.')
    from . import job_locks
    if not job_locks.acquire(ctx.root, source_id):
        raise LifecycleError(409, 'This source is busy. Try again in a moment.')
    try:
        files = managed_media(ctx, job)
        masters = [f for f, sid in master_dirs(ctx) if sid == source_id]
        if not files and not masters:
            return 0
        facts = {}
        source_path = job.get('source_path')
        if not is_link_source(job) and (not source_path or not Path(source_path).is_file()):
            raise LifecycleError(409, 'The original upload cannot be verified, so it cannot be freed safely.')
        if source_path and Path(source_path).is_file():
            facts['source_size'] = Path(source_path).stat().st_size
            if not is_link_source(job):
                facts['source_sha256'] = sha256_of(source_path)   # lets a re-dropped upload be matched
        freed = 0
        for path in files:
            freed += remove(ctx, path)
        upload = upload_dir(ctx, job)
        if upload and upload.is_dir() and not any(upload.iterdir()):
            upload.rmdir()
        for folder in masters:
            freed += remove(ctx, folder)
        for path in source_cache_paths(ctx, source_id):
            if path.name.startswith('evidence-'):
                continue                         # transcript evidence is small and needed to read the project
            freed += remove(ctx, path)
        provenance = job.get('provenance') or {}
        ctx.jobs.update(source_id, source_freed_at=iso(ctx.clock()), freed_provider_id=provenance.get('id'), **facts)
        COUNTERS['freed_sources'] += 1
        return freed
    finally:
        job_locks.release(ctx.root, source_id)


def prune_working(ctx, snap, collection):
    """Remove previews and thumbnails of a project; exports are kept."""
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    root = ctx.root / 'collection-artifacts'
    freed = 0
    for revision_id in snap.revision_ids(collection):
        folder = root / revision_id
        if folder.is_dir():
            for attempt in folder.iterdir():
                if not attempt.name.startswith('export-'):
                    freed += remove(ctx, attempt)
    thumbs = ctx.root / 'studio' / 'thumbnails'
    for short in snap.shorts(collection):
        for path in thumbs.glob(f'clip-{short["id"]}-*'):
            freed += remove(ctx, path)
    return freed


def free_source(ctx, project_id):
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    collection = require(ctx, project_id)
    snap = Snap(ctx)
    if snap.meta(project_id).get('keep_source'):
        raise LifecycleError(409, 'Source is kept for this project. Turn off Keep source first.')
    sharers = {u['id']: u for sid in collection['source_ids'] for u in snap.users.get(sid, [])}
    for user in sharers.values():
        if reason := snap.blocked(user):
            raise LifecycleError(409, reason)
        if user['id'] != project_id and snap.meta(user['id']).get('keep_source'):
            raise LifecycleError(409, 'Another project using this video keeps its source.')
    jobs = [j for j in snap.source_jobs(collection) if not source_is_freed(j)]
    if not any(managed_media(ctx, j) or any(sid == j['id'] for _, sid in snap.masters()) for j in jobs):
        raise LifecycleError(409, "This project's video is your own file, so Clippie has nothing to free.")
    for job in jobs:
        free_source_files(ctx, job['id'], snap)
    for user in sharers.values():
        prune_working(ctx, snap, user)
    return collection


def restore_source(ctx, project_id):
    collection = require(ctx, project_id)
    snap = Snap(ctx)
    if snap.is_trashed(collection):
        raise LifecycleError(409, TRASHED_MESSAGE)
    freed = [j for j in snap.source_jobs(collection) if source_is_freed(j)]
    if not freed:
        raise LifecycleError(409, "This project's source is not freed.")
    plan = []
    for job in freed:
        if job['status'] in BUSY_SOURCE:
            continue                                     # a restore is already running
        if not is_link_source(job):
            raise LifecycleError(422, UPLOAD_MESSAGE)
        plan.append(job)
    for job in plan:
        ctx.jobs.update(job['id'], status='failed', error=None, progress='Restoring the video')
        if ctx.retry is None:
            raise LifecycleError(503, 'Restoring is not available right now.')
        ctx.retry(job['id'])
    touch(ctx, project_id)
    return collection


def matches_upload(ctx, source_id, path):
    """True when a re-dropped file is the original this upload source was freed from."""
    job = ctx.jobs.get(source_id)
    expected = job.get('source_sha256')
    return bool(expected) and Path(path).is_file() and sha256_of(path) == expected


def reconcile(ctx):
    """Clear freed markers once a restore has brought the file back; refuse a link whose video changed."""
    count = 0
    for job in ctx.jobs.all():
        if not job.get('source_freed_at'):
            continue
        path = job.get('source_path')
        if job.get('status') != 'ready' or not path or not Path(path).is_file():
            continue
        if not is_link_source(job):
            continue  # Upload restoration commits only after its preview is prepared.
        wanted, found = job.get('freed_provider_id'), (job.get('provenance') or {}).get('id')
        if not wanted or not found or str(wanted) != str(found):
            ctx.jobs.update(job['id'], status='failed', error='The restored video identity could not be verified. Add it again.')
        elif not job.get('preview_path') or not Path(job['preview_path']).is_file():
            continue
        else:
            ctx.jobs.update(job['id'], source_freed_at=None)
        count += 1
    return count


# -- purge and dismiss -----------------------------------------------------------------

def purge_source(ctx, source_id):
    """Remove a source completely: media, job record, caches, masters and upload row. Returns bytes freed."""
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    from . import job_locks
    if not job_locks.acquire(ctx.root, source_id):
        raise LifecycleError(409, 'This source is busy. Try again in a moment.')
    try:
        freed = 0
        try:
            job = ctx.jobs.get(source_id)
        except KeyError:
            job = None
        if job:
            upload = upload_dir(ctx, job)
            if upload:
                freed += remove(ctx, upload)
        freed += remove(ctx, ctx.root / source_id)
        for folder, sid in master_dirs(ctx):
            if sid == source_id:
                freed += remove(ctx, folder)
        for path in source_cache_paths(ctx, source_id):
            freed += remove(ctx, path)
        with ctx.studio._db() as db:
            db.execute('DELETE FROM uploads WHERE id=?', (source_id,))
        if job:
            ctx.jobs.delete(source_id)
        return freed
    finally:
        job_locks.release(ctx.root, source_id)


def dismiss_source(ctx, source_id):
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    try:
        job = ctx.jobs.get(source_id)
    except KeyError:
        raise LifecycleError(404, 'Source not found.') from None
    if job['status'] in BUSY_SOURCE:
        raise LifecycleError(409, 'This source is still being prepared. Cancel it first.')
    if any(source_id in c['source_ids'] for c in ctx.collections.list_collections()):
        raise LifecycleError(409, 'This source is used by a project. Delete the project first.')
    purge_source(ctx, source_id)


def purge_project(ctx, project_id):
    """Permanently remove a trashed project. Posts survive, labelled with the project's name."""
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    collection = require(ctx, project_id)
    snap = Snap(ctx)
    if not snap.is_trashed(collection):
        raise LifecycleError(409, 'Move the project to the trash before deleting it.')
    if reason := snap.blocked(collection):
        raise LifecycleError(409, reason)
    shorts = snap.shorts(collection)
    short_ids = {s['id'] for s in shorts}
    revisions = snap.revision_ids(collection)
    name = ctx.project_name(collection) or 'Deleted project'
    store = ctx.publish_store()
    if store is not None:
        from .publish.store import snapshot_project_names
        snapshot_project_names(project_id, name, short_ids, store=store)   # before shorts disappear
    # files first, records after: a failure leaves a project that can be purged again
    for revision_id in revisions:
        remove(ctx, ctx.root / 'collection-artifacts' / revision_id)
        remove(ctx, ctx.root / 'collection-artifacts' / 'studio' / revision_id)
    for short_id in short_ids:
        for path in (ctx.root / 'studio' / 'thumbnails').glob(f'clip-{short_id}-*'):
            remove(ctx, path)
    for path in (ctx.root / 'collection-diagnostics').glob(f'{project_id}-*'):
        remove(ctx, path)
    remove(ctx, ctx.root / 'collection-coordinators' / f'{project_id}.lock')
    with ctx.studio.lock, ctx.studio._db() as db:
        db.execute('DELETE FROM project_options WHERE id=?', (project_id,))
        for clip_id in short_ids:
            db.execute('DELETE FROM exports WHERE json_extract(document, "$.clip_id")=?', (clip_id,))
            for row in db.execute('SELECT id FROM jobs WHERE json_extract(document, "$.clip_id")=?', (clip_id,)).fetchall():
                db.execute('DELETE FROM job_events WHERE job_id=?', (row[0],))
            db.execute('DELETE FROM jobs WHERE json_extract(document, "$.clip_id")=?', (clip_id,))
    ctx.collections.delete_collection(project_id)
    ctx.delete_meta(project_id)
    # sources nobody else references go with the project
    remaining = {sid for c in ctx.collections.list_collections() for sid in c['source_ids']}
    for source_id in collection['source_ids']:
        if source_id not in remaining:
            try:
                job = ctx.jobs.get(source_id)
            except KeyError:
                continue
            if job['status'] in BUSY_SOURCE:
                continue
            try:
                purge_source(ctx, source_id)
            except LifecycleError:
                LOG.warning('Source %s is busy; the sweeper will retry', source_id)
    COUNTERS['purged_projects'] += 1


# -- master GC -------------------------------------------------------------------------

def collect_masters(ctx, dry_run=False, orphan_age=24 * 3600):
    """Delete HD masters whose source is gone or freed. Returns (bytes, folders)."""
    if not dry_run and not DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, DELETION_PAUSED)
    total, removed = 0, []
    for folder, source_id in master_dirs(ctx):
        if source_id is None:
            try:
                young = ctx.clock() - max(folder.stat().st_mtime, ctx.rollout_at()) < orphan_age
            except OSError:
                continue
            if young:
                continue                              # a master may be downloading right now
        else:
            try:
                if not source_is_freed(ctx.jobs.get(source_id)):
                    continue
            except KeyError:
                pass                                  # the source no longer exists
        lock = folder / 'lock'
        handle = None
        try:
            if lock.exists():
                handle = lock.open('a')
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue                          # in use
            total += tree_bytes(folder)
            removed.append(str(folder))
            if not dry_run:
                remove(ctx, folder)
        finally:
            if handle:
                handle.close()
    if not dry_run:
        COUNTERS['removed_masters'] += len(removed)
    return total, removed


# -- sweeper ---------------------------------------------------------------------------

def cleanup_restore_staging(ctx):
    """Remove abandoned restore files, skipping sources locked by another process."""
    from . import job_locks
    base = ctx.root / 'restore-staging'
    if not base.is_dir() or base.is_symlink():
        return
    for folder in base.iterdir():
        if not re.fullmatch(r'[A-Za-z0-9_-]+', folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        if not job_locks.acquire(ctx.root, folder.name):
            continue
        try:
            shutil.rmtree(folder)
        finally:
            job_locks.release(ctx.root, folder.name)


def sweep(ctx, dry_run=False):
    """One idempotent pass. Takes per-source job locks, never touches busy or pinned work."""
    if not DESTRUCTIVE_LIFECYCLE_ENABLED:
        if not dry_run:
            cleanup_restore_staging(ctx)
            reconcile(ctx)
        return {'dry_run': dry_run, 'purged_projects': [], 'dismissed_sources': [], 'freed_sources': [],
                'removed_masters': [], 'reclaimable_bytes': 0, 'errors': []}
    now, days = ctx.clock(), ctx.retention_days()
    report = {'dry_run': dry_run, 'purged_projects': [], 'dismissed_sources': [], 'freed_sources': [],
              'removed_masters': [], 'reclaimable_bytes': 0, 'errors': []}
    if not dry_run:
        cleanup_restore_staging(ctx)
        reconcile(ctx)
    snap = Snap(ctx)
    # 1. trash past its grace period
    for collection in snap.collections:
        trashed = snap.trashed_at(collection)
        if trashed and trashed + TRASH_DAYS * 86400 <= now and not snap.blocked(collection):
            working, exports = snap.project_sizes(collection)
            report['purged_projects'].append(collection['id'])
            report['reclaimable_bytes'] += working + exports
            if not dry_run:
                try:
                    purge_project(ctx, collection['id'])
                except Exception as error:
                    report['errors'].append(f'purge {collection["id"]}: {error}')
    if not dry_run and report['purged_projects']:
        snap = Snap(ctx)
    # 2. failed or cancelled sources nobody uses
    for job in snap.jobs.values():
        if not is_link_source(job) or job['status'] not in {'failed', 'cancelled'} or snap.users.get(job['id']):
            continue
        stamp = max(parse_time(job.get('created_at')) or 0, _mtime(ctx.root / job['id']), ctx.rollout_at())
        if stamp and now - stamp > DISMISS_DAYS * 86400:
            report['dismissed_sources'].append(job['id'])
            report['reclaimable_bytes'] += snap.source_bytes(job)
            if not dry_run:
                try:
                    purge_source(ctx, job['id'])
                    COUNTERS['dismissed_sources'] += 1
                except Exception as error:
                    report['errors'].append(f'dismiss {job["id"]}: {error}')
    # 3. idle sources
    if days:
        cutoff = days * 86400
        for job in list(snap.jobs.values()):
            if job['id'] in report['dismissed_sources'] or job['status'] != 'ready' or source_is_freed(job):
                continue
            if not is_link_source(job):
                continue  # Uploaded originals are never automatically freed.
            if not managed_media(ctx, job) and not any(sid == job['id'] for _, sid in snap.masters()):
                continue
            users = snap.users.get(job['id'], [])
            if users:
                idle = all(now - snap.idle_since(u) > cutoff and not snap.meta(u['id']).get('keep_source')
                           and not snap.blocked(u) for u in users)
            else:
                created = parse_time(job.get('created_at'))
                idle = created is not None and now - max(created, _mtime(ctx.root / job['id']), ctx.rollout_at()) > cutoff
            if not idle:
                continue
            report['freed_sources'].append(job['id'])
            report['reclaimable_bytes'] += snap.source_bytes(job)
            if not dry_run:
                try:
                    free_source_files(ctx, job['id'], snap)
                    for user in users:
                        prune_working(ctx, snap, user)
                except LifecycleError as error:
                    report['errors'].append(f'free {job["id"]}: {error.message}')
    # 4. masters
    bytes_, folders = collect_masters(ctx, dry_run=dry_run)
    report['removed_masters'] = folders
    freed_ids = set(report['freed_sources'])
    if dry_run:
        # a master of a source about to be freed is already counted with that source
        bytes_ = sum(tree_bytes(f) for f, sid in snap.masters()
                     if (sid is None or sid not in snap.jobs or source_is_freed(snap.jobs[sid])) and str(f) in folders and sid not in freed_ids)
    report['reclaimable_bytes'] += bytes_
    if not dry_run:
        COUNTERS['sweeps'] += 1
        COUNTERS['errors'] += len(report['errors'])
        LOG.info('lifecycle sweep: %s', {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()})
    return report


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0


# -- storage payload -------------------------------------------------------------------

def storage(ctx):
    root = ctx.root
    total = tree_bytes(root)
    snap = Snap(ctx)
    sources = sum(tree_bytes(p) for job in snap.jobs.values() for p in managed_media(ctx, job))
    sources += sum(tree_bytes(root / n) for n in ('final-sources', 'sources'))
    exports = tree_bytes(root / 'collection-artifacts' / 'studio')
    artifacts = root / 'collection-artifacts'
    working = 0
    if artifacts.is_dir():
        for revision in artifacts.iterdir():
            if revision.name == 'studio' or not revision.is_dir():
                continue
            for attempt in revision.iterdir():
                size = tree_bytes(attempt)
                if attempt.name.startswith('export-'):
                    exports += size
                else:
                    working += size
    working += sum(tree_bytes(root / n) for n in ('studio', 'analysis-v3', 'source-evidence', 'source-visuals', 'topic-maps'))
    models = tree_bytes(root / 'models')
    dry = sweep(ctx, dry_run=True)
    return {'total_bytes': total, 'sources_bytes': sources, 'working_bytes': working, 'exports_bytes': exports,
            'models_bytes': models, 'other_bytes': max(0, total - sources - working - exports - models),
            'reclaimable_bytes': dry['reclaimable_bytes'], 'retention_days': ctx.retention_days()}


# -- background ------------------------------------------------------------------------

def start_background(make_ctx, stop, first_delay=0.0):
    """Sweep at startup and every six hours until `stop` is set. Failures are logged, never raised."""
    def loop():
        wait = first_delay
        while not stop.wait(wait):
            try:
                sweep(make_ctx())
            except Exception:
                COUNTERS['errors'] += 1
                LOG.exception('Lifecycle sweep failed')
            wait = SWEEP_SECONDS
    thread = threading.Thread(target=loop, name='clippie-lifecycle', daemon=True)
    thread.start()
    return thread
