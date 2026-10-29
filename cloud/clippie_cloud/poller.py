"""Polls tracked posts on the decaying schedule 1 h, 6 h, 24 h, 72 h, 7 d, 28 d after publication."""
import logging
import threading
import time

from . import youtube
from .crypto import VaultError

LOG = logging.getLogger('clippie_cloud')
HOUR = 3600.0
OFFSETS = (1 * HOUR, 6 * HOUR, 24 * HOUR, 72 * HOUR, 7 * 24 * HOUR, 28 * 24 * HOUR)
SLACK = 120.0
BACKOFF = 900.0


def next_stage(base, stage, now):
    stage += 1
    while stage < len(OFFSETS) and base + OFFSETS[stage] <= now:
        stage += 1
    return (stage, base + OFFSETS[stage]) if stage < len(OFFSETS) else (stage, None)


class Poller:
    def __init__(self, store, vault, http_factory, now=time.time):
        self.store, self.vault, self.http_factory, self.now = store, vault, http_factory, now
        self.flight = threading.Lock()
        self.stop, self.thread = threading.Event(), None

    def run_once(self):
        if not self.flight.acquire(blocking=False):
            return None
        try:
            http = self.http_factory()
            try:
                due = self.store.due_posts(self.now())
                for post in due:
                    self.poll(http, post)
                return len(due)
            finally:
                http.close()
        finally:
            self.flight.release()

    def poll(self, http, post):
        device, post_id, now = post['device_id'], post['post_id'], self.now()
        try:
            account = self.store.get_account(device, post['channel_id'])
            if account is None:
                return self.store.update_post(device, post_id, next_poll_at=None, last_error='Channel was removed.')
            token = youtube.refresh(http, self.vault.open(account['sealed']))
            state = youtube.video_state(http, token, post['external_id'])
            if state is None:
                return self.store.update_post(device, post_id, next_poll_at=None, last_error='The video no longer exists.')
            privacy, publish_at, stats = state
            if privacy != 'public':
                wait = max((post['scheduled_at'] or publish_at or now) - now, 0) + SLACK
                return self.store.update_post(device, post_id, next_poll_at=now + max(wait, 300.0), last_error=None)
            published = post['published_at'] or publish_at or post['scheduled_at'] or now
            if not post['published_at']:
                self.store.update_post(device, post_id, published_at=published)
                if published + OFFSETS[0] > now:  # first metrics wait for the 1 h mark
                    return self.store.update_post(device, post_id, stage=0, next_poll_at=published + OFFSETS[0], last_error=None)
            values, curve = youtube.metrics(http, token, post['external_id'], published, now, stats)
            self.store.add_snapshot(device, post_id, now, values, curve)
            stage, when = next_stage(published, post['stage'], now)
            self.store.update_post(device, post_id, stage=stage, next_poll_at=when, attempts=0, last_error=None)
        except (youtube.UpstreamError, VaultError) as error:
            attempts = post['attempts'] + 1
            delay = min(BACKOFF * 2 ** (attempts - 1), 6 * HOUR)
            self.store.update_post(device, post_id, attempts=attempts, last_error=str(error), next_poll_at=now + delay)

    def start(self, interval):
        def loop():
            while not self.stop.is_set():
                try:
                    self.run_once()
                except Exception:
                    LOG.exception('poll pass failed')
                self.stop.wait(interval)
        self.thread = threading.Thread(target=loop, name='clippie-cloud-poller', daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
