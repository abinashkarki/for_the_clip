"""Scheduler: backoff, durability across restarts, single-flight, decaying metric polls, background thread."""
import threading
import time

import pytest

from publish_support import (connect_aggregator, connect_youtube, fake_engine, finished_post, publish,  # noqa: F401
                             studio, video_bytes, wait_for_render)

from clippie.publish import runtime, sync
from clippie.publish.base import POLL_OFFSETS
from clippie.publish.store import PublishStore


def fail_inserts(fakes):
    """Make every YouTube resumable-session start fail with a 503."""
    original = fakes.google.handle

    def handle(request):
        import httpx
        if request.url.path == '/upload/youtube/v3/videos':
            return httpx.Response(503, json={'error': {'message': 'backend error'}})
        return original(request)
    fakes.google.handle = handle
    return original


def test_upload_failures_back_off_then_fail_after_six_attempts(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    fail_inserts(publish)
    clock = [time.time()]
    publish.service.now = lambda: clock[0]
    delays = []
    for attempt in range(1, 7):
        publish.service.run_once()
        row = publish.db.get_post(post['id'])
        assert row['attempts'] == attempt and 'YouTube error' in row['error']
        if row['status'] == 'failed':
            break
        delays.append(row['next_attempt_at'] - clock[0])
        publish.service.run_once()  # not due yet: nothing changes
        assert publish.db.get_post(post['id'])['attempts'] == attempt
        clock[0] = row['next_attempt_at'] + 1
    assert delays == [60, 300, 900, 1800, 3600]
    assert row['status'] == 'failed' and row['next_attempt_at'] is None


def test_auth_errors_fail_immediately_without_retry(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    publish.google.token_error = 'Token has been expired or revoked.'
    publish.service.run_once()
    row = publish.db.get_post(post['id'])
    assert row['status'] == 'failed' and row['attempts'] == 1 and 'revoked' in row['error']


def test_missing_credentials_fail_the_post_with_guidance(publish, isolated_credential_store):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    isolated_credential_store.values.clear()
    publish.service.run_once()
    row = publish.db.get_post(post['id'])
    assert row['status'] == 'failed' and 'OAuth client' in row['error']


def test_partial_upload_resumes_the_saved_session_after_a_restart(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    google = publish.google
    google.fail = {i: 'before' for i in range(1, 30)}  # first chunk lands, then YouTube fails
    import clippie.publish.runtime as runtime_module
    original = runtime_module.youtube_client
    runtime_module.youtube_client = lambda http, db, channel=None, **kw: original(http, db, channel, chunk_size=1024, retries=1, **kw)
    try:
        publish.service.run_once()
        row = publish.db.get_post(post['id'])
        assert row['status'] == 'scheduled' and row['upload_session'] == 'https://upload.test/session1'
        assert 0 < len(google.received) < 4000
        # "restart": a new store and service object read the same database
        runtime.reset()
        sync.stop_all()
        service = sync.service(publish.root)
        service.now = lambda: time.time() + 4000
        google.fail = {}
        service.run_once()
    finally:
        runtime_module.youtube_client = original
    row = runtime.store(publish.root).get_post(post['id'])
    assert row['external_id'] == 'vid123' and google.sessions == 1 and row['upload_session'] is None


def test_opening_store_does_not_recover_live_upload(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    publish.db.update_post(post['id'], status='uploading')
    reopened = PublishStore(publish.root)
    assert reopened.get_post(post['id'])['status'] == 'uploading'
    reopened.recover()  # coordinator holds the publishing lock when invoking this
    row = reopened.get_post(post['id'])
    assert row['status'] == 'failed' and 'uncertain' in row['error']


def test_expired_session_never_starts_a_duplicate_upload(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    publish.db.update_post(post['id'], upload_session='https://upload.test/stale')
    publish.google.expire_session = True
    publish.service.run_once()
    row = publish.db.get_post(post['id'])
    assert row['upload_session'] and row['attempts'] == 1 and row['status'] == 'failed'
    assert 'uncertain' in row['error'] and publish.google.sessions == 0
    retry = publish.studio.client.post(f'/api/publish/posts/{post["id"]}/retry')
    assert retry.status_code == 409 and publish.google.sessions == 0
    rejected = publish.studio.client.post(f'/api/publish/posts/{post["id"]}/resolve-uncertain',
                                         json={'checked_not_uploaded': False})
    assert rejected.status_code == 409
    accepted = publish.studio.client.post(f'/api/publish/posts/{post["id"]}/resolve-uncertain',
                                         json={'checked_not_uploaded': True})
    assert accepted.status_code == 200 and publish.db.get_post(post['id'])['status'] == 'failed'
    assert publish.db.get_post(post['id'])['upload_session'] is None and publish.google.sessions == 0


def test_video_id_survives_status_failure_without_reupload(publish, monkeypatch):
    from clippie.publish.base import PublishError
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    class Adapter:
        def upload(self, *args): return {'video_id': 'remote-id'}
        def schedule(self, *args): raise PublishError('Temporary status failure', retryable=True)
    monkeypatch.setattr(runtime, 'publisher', lambda *args, **kwargs: Adapter())
    publish.service.run_once()
    saved = publish.db.get_post(post['id'])
    assert saved['external_id'] == 'remote-id' and saved['status'] == 'scheduled'
    assert saved['next_poll_at'] and saved['upload_session'] is None
    assert not publish.db.due_uploads(time.time() + 10000)


def test_finished_export_on_failed_post_is_visible_without_upload(publish):
    from clippie import studio_api
    connect_youtube(publish)
    post, short, _ = finished_post(publish)
    job = next(j for j in studio_api.studio().list_jobs() if j.get('clip_id') == short['id'])
    publish.db.update_post(post['id'], status='failed', artifact_id=None, render_job_id=job['id'],
                           error='The export failed: The app stopped during this job.')
    result = publish.studio.client.get(f'/api/publish/posts/{post["id"]}').json()
    assert result['export_recovered'] and result['export_preview_url'] == job['result']['video_url']
    assert result['status'] == 'failed' and not result['rendering']
    assert publish.google.sessions == 0


def test_publishing_lock_blocks_another_coordinator(publish):
    import fcntl
    lock = (publish.root / 'publish-sync.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert publish.service.run_once() is None
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def test_single_flight_skips_a_pass_while_one_is_running(publish):
    connect_youtube(publish)
    release, entered = threading.Event(), threading.Event()
    original = publish.service.attach_renders

    def slow():
        entered.set()
        release.wait(5)
        return original()
    publish.service.attach_renders = slow
    results = []
    thread = threading.Thread(target=lambda: results.append(publish.service.run_once()))
    thread.start()
    assert entered.wait(5)
    assert publish.service.run_once() is None
    release.set()
    thread.join(5)
    assert results[0] == {'renders': 0, 'uploads': 0, 'polls': 0}


def test_failed_render_fails_the_post_with_the_reason(publish):
    connect_youtube(publish)
    publish.studio.engine.state['render_error'] = 'ffmpeg exploded'
    _, short, revision = publish.studio.clip()
    reply = publish.studio.client.post('/api/publish/posts', json={
        'short_id': short['id'], 'revision_id': revision['id'], 'platforms': ['youtube'], 'title': 'T'})
    post = reply.json()['posts'][0]
    publish.studio.wait(publish.db.get_post(post['id'])['render_job_id'])
    publish.service.attach_renders()
    row = publish.db.get_post(post['id'])
    assert row['status'] == 'failed' and 'ffmpeg exploded' in row['error']


def test_metric_polls_follow_the_decaying_schedule_and_end_after_28_days(publish):
    connect_youtube(publish)
    publish.google.video = {'privacy': 'public', 'publishAt': None}
    post, _, _ = finished_post(publish)
    clock = [time.time()]
    publish.service.now = lambda: clock[0]
    publish.service.run_once()
    row = publish.db.get_post(post['id'])
    base = row['published_at']
    assert row['next_poll_at'] == pytest.approx(base + POLL_OFFSETS[0])
    seen = []
    for expected in POLL_OFFSETS:
        row = publish.db.get_post(post['id'])
        assert row['next_poll_at'] == pytest.approx(base + expected)
        publish.service.run_once()  # too early
        assert len(publish.db.snapshots(post['id'])) == len(seen)
        clock[0] = row['next_poll_at'] + 1
        publish.service.run_once()
        seen.append(clock[0])
        assert len(publish.db.snapshots(post['id'])) == len(seen)
    row = publish.db.get_post(post['id'])
    assert row['next_poll_at'] is None and row['poll_stage'] == len(POLL_OFFSETS)
    assert [round(o / 3600) for o in POLL_OFFSETS] == [1, 6, 24, 72, 168, 672]
    assert publish.db.latest_retention(post['id'])['points'][0] == [0.0, 1.0, 0.5]


def test_polling_after_long_downtime_skips_stages_that_already_passed(publish):
    connect_youtube(publish)
    publish.google.video = {'privacy': 'public', 'publishAt': None}
    post, _, _ = finished_post(publish)
    publish.service.run_once()
    base = publish.db.get_post(post['id'])['published_at']
    publish.service.now = lambda: base + 100 * 3600  # laptop slept for four days
    publish.service.run_once()
    row = publish.db.get_post(post['id'])
    assert len(publish.db.snapshots(post['id'])) == 1 and row['poll_stage'] == 4
    assert row['next_poll_at'] == pytest.approx(base + POLL_OFFSETS[4])


def test_poll_errors_back_off_exponentially_and_recover(publish):
    connect_youtube(publish)
    publish.google.video = {'privacy': 'public', 'publishAt': None}
    post, _, _ = finished_post(publish)
    publish.service.run_once()
    base = publish.db.get_post(post['id'])['published_at']
    clock = [base + 3601]
    publish.service.now = lambda: clock[0]
    publish.google.analytics_error = 500
    gaps = []
    for _ in range(3):
        publish.service.run_once()
        row = publish.db.get_post(post['id'])
        gaps.append(row['next_poll_at'] - clock[0])
        assert row['status'] == 'published' and row['error'].startswith('Metrics:') and row['poll_stage'] == 0
        clock[0] = row['next_poll_at'] + 1
    assert gaps == [900, 1800, 3600]
    publish.google.analytics_error = None
    publish.service.run_once()
    row = publish.db.get_post(post['id'])
    assert row['error'] is None and row['attempts'] == 0 and len(publish.db.snapshots(post['id'])) == 1


def test_video_deleted_on_youtube_fails_the_scheduled_post(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish, when=None)
    publish.service.run_once()
    original = publish.google.handle

    def gone(request):
        import httpx
        if request.url.path == '/youtube/v3/videos':
            return httpx.Response(200, json={'items': []})
        return original(request)
    publish.google.handle = gone
    publish.service.now = lambda: time.time() + 3600
    publish.service.run_once()
    row = publish.db.get_post(post['id'])
    assert row['status'] == 'failed' and 'no record' in row['error'] and row['next_poll_at'] is None


def test_aggregator_scheduled_post_is_published_once_the_service_reports_success(publish):
    connect_aggregator(publish)
    publish.aggregator.status_value = 'pending'
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) + timedelta(hours=3)
    _, short, revision = publish.studio.clip()
    reply = publish.studio.client.post('/api/publish/posts', json={
        'short_id': short['id'], 'revision_id': revision['id'], 'platforms': ['instagram'], 'title': 'Reel',
        'scheduled_at': when.strftime('%Y-%m-%dT%H:%M:%SZ')})
    post = reply.json()['posts'][0]
    wait_for_render(publish, post['id'])
    publish.service.run_once()
    assert publish.db.get_post(post['id'])['status'] == 'scheduled' and publish.aggregator.posted[0]['scheduleDate']
    publish.aggregator.status_value = 'success'
    publish.service.now = lambda: when.timestamp() + 600
    publish.service.run_once()
    assert publish.db.get_post(post['id'])['status'] == 'published'
    publish.service.now = lambda: when.timestamp() + 4200
    publish.service.run_once()
    assert publish.db.latest_snapshot(post['id'])['metrics']['views'] == 900


def test_background_thread_uploads_on_kick_and_stops(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    service = sync.SyncService(publish.root)
    service.start()
    service.wake.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not publish.db.get_post(post['id'])['external_id']:
        time.sleep(0.05)
    service.shutdown()
    assert publish.db.get_post(post['id'])['external_id'] == 'vid123' and not service.thread.is_alive()


def test_background_start_only_when_publishing_is_in_use(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(sync, 'ensure_started', started.append)
    sync.start_background(tmp_path)
    assert started == []
    PublishStore(tmp_path)
    sync.start_background(tmp_path)
    assert started == [tmp_path]
