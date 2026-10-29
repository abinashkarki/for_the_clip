"""Export job lifecycle with a fake renderer, artifact files with Range, containment, restart recovery."""
from pathlib import Path
import time

import pytest

from test_studio_support import fake_engine, png_bytes, studio, video_bytes, v2_timeline  # noqa: F401

from clippie import app as module, studio_api
from clippie.collection_api import store
from clippie.studio_store import INTERRUPTED, StudioStore

API = '/api/studio'


def export(studio, short, revision_id, quality='preview'):
    return studio.client.post(f'{API}/clips/{short["id"]}/export', json={'revision_id': revision_id, 'quality': quality})


def test_export_job_renders_commits_an_artifact_and_serves_files(studio):
    _, short, revision = studio.clip()
    queued = export(studio, short, revision['id'])
    assert queued.status_code == 202
    job = studio.wait(queued.json()['job_id'])
    assert job['status'] == 'done' and job['progress'] == 1.0 and job['error'] is None and job['kind'] == 'export'
    result = job['result']
    assert set(result) == {'video_url', 'srt_url', 'duration'} and result['duration'] == 8.0
    render = studio.engine.renders[0]
    assert render['preview'] is True and render['timeline']['schema_version'] == 3
    artifacts = studio.root / 'collection-artifacts' / 'studio' / revision['id']
    assert Path(render['directory']).parent == artifacts
    video = studio.client.get(result['video_url'])
    assert video.status_code == 200 and video.headers['content-type'] == 'video/mp4' and video.content[4:8] == b'ftyp'
    ranged = studio.client.get(result['video_url'], headers={'Range': 'bytes=0-15'})
    assert ranged.status_code == 206 and len(ranged.content) == 16
    assert ranged.headers['content-range'].startswith('bytes 0-15/')
    assert studio.client.get(result['srt_url']).text.startswith('1\n00:00:00,000')
    detail = studio.detail(short['id'])
    assert detail['clip']['preview_url'] == result['video_url']
    project = studio.client.get(f'{API}/projects/{short["collection_id"]}').json()
    assert project['clips'][0]['preview_url'] == result['video_url']
    artifact = studio_api.studio().get_export(result['video_url'].split('/')[-2])
    assert artifact['kind'] == 'studio_export' and artifact['revision_id'] == revision['id'] and len(artifact['sha256']) == 64


def test_1080p_export_renders_full_size_and_resolves_logo_assets(studio):
    _, short, revision = studio.clip()
    asset = studio.client.post(f'{API}/brand-assets', files={'file': ('logo.png', png_bytes(), 'image/png')}).json()
    kit = studio.client.post(f'{API}/brand-kits', json={'name': 'K', 'template_id': 'bold_pop',
                                                        'progress_bar': {'enabled': False, 'color': '#FFFFFF'},
                                                        'logo_asset_id': asset['asset_id']}).json()
    applied = studio.client.post(f'{API}/clips/{short["id"]}/actions/apply-brand-kit',
                                 json={'base_revision': revision['id'], 'request_id': 'k', 'brand_kit_id': kit['id']}).json()
    assert studio.wait(export(studio, short, applied['revision_id'], '1080p').json()['job_id'])['status'] == 'done'
    render = studio.engine.renders[0]
    assert render['preview'] is False
    path = render['assets'][asset['asset_id']]
    assert Path(path).is_file() and Path(path).read_bytes() == png_bytes()


def test_export_reports_running_state_and_progress_while_rendering(studio):
    _, short, revision = studio.clip()
    studio.engine.state['render_delay'] = 0.4
    job_id = export(studio, short, revision['id']).json()['job_id']
    seen = set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = studio.client.get(f'{API}/jobs/{job_id}').json()
        seen.add(job['status'])
        if job['status'] == 'done':
            break
        time.sleep(0.02)
    assert seen <= {'queued', 'running', 'done'} and 'running' in seen and job['status'] == 'done'


def test_export_runs_on_the_single_app_worker(studio, monkeypatch):
    _, short, revision = studio.clip()
    submitted = []

    class Recorder:
        def submit(self, function, *args):
            submitted.append((function, args))

        def shutdown(self, **kwargs):
            pass
    monkeypatch.setattr(module, 'WORKER', Recorder())
    job_id = export(studio, short, revision['id']).json()['job_id']
    assert len(submitted) == 1 and studio.client.get(f'{API}/jobs/{job_id}').json()['status'] == 'queued'
    submitted[0][0](*submitted[0][1])
    assert studio.client.get(f'{API}/jobs/{job_id}').json()['status'] == 'done'


def test_render_failure_marks_the_job_failed_without_an_artifact(studio):
    _, short, revision = studio.clip()
    studio.engine.state['render_error'] = 'ffmpeg exited 1'
    job = studio.wait(export(studio, short, revision['id']).json()['job_id'])
    assert job['status'] == 'failed' and 'ffmpeg exited 1' in job['error'] and job['result'] is None
    assert studio.detail(short['id'])['clip']['preview_url'] is None


def test_a_stopped_worker_fails_the_job_instead_of_hanging(studio, monkeypatch):
    _, short, revision = studio.clip()
    monkeypatch.setattr(module.WORKER, 'submit', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('shutdown')))
    job = studio.client.get(f'{API}/jobs/{export(studio, short, revision["id"]).json()["job_id"]}').json()
    assert job['status'] == 'failed' and 'shutting down' in job['error']


def test_export_validates_clip_revision_and_quality(studio):
    _, short, revision = studio.clip()
    _, other, other_revision = studio.clip()
    url = f'{API}/clips/{short["id"]}/export'
    assert studio.client.post(f'{API}/clips/missing/export', json={'revision_id': revision['id'], 'quality': 'preview'}).status_code == 404
    assert export(studio, short, 'nope').status_code == 404
    assert export(studio, short, other_revision['id']).status_code == 404
    assert export(studio, short, '../x').status_code == 404
    assert studio.client.post(url, json={'revision_id': revision['id'], 'quality': '4k'}).status_code == 422
    assert studio.client.post(url, json={'revision_id': revision['id']}).status_code == 422
    assert studio.client.post(url, json={'revision_id': revision['id'], 'quality': 'preview', 'x': 1}).status_code == 422
    assert studio.engine.renders == []


def test_an_earlier_revision_can_be_exported(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    timeline['captions']['template_id'] = 'color_word'
    studio.client.put(f'{API}/clips/{short["id"]}/timeline', json={'base_revision': revision['id'], 'timeline': timeline, 'request_id': 'e'})
    job = studio.wait(export(studio, short, revision['id']).json()['job_id'])
    assert job['status'] == 'done'
    assert studio.engine.renders[0]['timeline']['captions']['template_id'] == 'boxed_word'


def test_jobs_endpoint_shape_and_unknown_ids(studio):
    assert studio.client.get(f'{API}/jobs/unknown').status_code == 404
    assert studio.client.get(f'{API}/jobs/..%2fx').status_code == 404
    job = studio_api.studio().create_job('export')
    assert studio.client.get(f'{API}/jobs/{job["id"]}').json() == {
        'id': job['id'], 'kind': 'export', 'status': 'queued', 'progress': 0.0, 'error': None, 'result': None}


def test_store_open_preserves_live_jobs_and_recovery_only_claims_abandoned_jobs(studio):
    from clippie import job_locks
    queued = studio_api.studio().create_job('export')
    running = studio_api.studio().create_job('auto_reframe')
    studio_api.studio().update_job(running['id'], status='running', progress=0.4)
    done = studio_api.studio().create_job('export')
    studio_api.studio().update_job(done['id'], status='done', progress=1.0, result={'video_url': 'x'})
    studio_api._STORES.clear()  # Opening another store is not a restart.
    for identity in (queued['id'], running['id']):
        job = studio.client.get(f'{API}/jobs/{identity}').json()
        assert job['status'] in {'queued', 'running'}
    job_locks.release(studio.root, running['id'])  # simulate owner process exit
    studio_api.recover_jobs()
    assert studio.client.get(f'{API}/jobs/{running["id"]}').json()['status'] == 'failed'
    assert studio.client.get(f'{API}/jobs/{queued["id"]}').json()['status'] == 'queued'
    assert studio.client.get(f'{API}/jobs/{done["id"]}').json()['status'] == 'done'
    assert StudioStore(studio.root).get_job(done['id'])['result'] == {'video_url': 'x'}


def test_abandoned_export_is_requeued_with_original_inputs(studio):
    from clippie import job_locks
    _, short, revision = studio.clip()
    _, collection = studio_api.clip_context(short['id'])
    sources = studio_api.revision_sources(collection, revision)
    timeline = studio_api.engine.validate(studio_api.timeline_for(revision, collection, sources), sources)
    assets = studio_api.asset_paths(timeline)
    job = studio_api.studio().create_job('export', short['id'], {
        'revision_id': revision['id'], 'quality': 'preview',
        'input_fingerprint': studio_api.export_input_fingerprint(timeline, sources, assets)})
    job_locks.release(studio.root, job['id'])
    studio_api.recover_jobs()
    result = studio.wait(job['id'])
    assert result['status'] == 'done'
    saved = studio_api.studio().get_job(job['id'])
    assert saved['attempt'] == 2 and saved['error'] is None
    assert len(studio.engine.renders) == 1


def test_recovery_refuses_changed_export_inputs(studio):
    from clippie import job_locks
    _, short, revision = studio.clip()
    job = studio_api.studio().create_job('export', short['id'], {
        'revision_id': revision['id'], 'quality': 'preview', 'input_fingerprint': 'old-inputs'})
    job_locks.release(studio.root, job['id'])
    studio_api.recover_jobs()
    saved = studio_api.studio().get_job(job['id'])
    assert saved['status'] == 'failed' and 'changed' in saved['error']
    assert not studio.engine.renders


def test_job_owner_is_visible_to_another_process(studio):
    import subprocess
    import sys
    from clippie import job_locks
    job = studio_api.studio().create_job('export')
    code = 'from clippie import job_locks; import sys; print(job_locks.acquire(sys.argv[1], sys.argv[2]))'
    try:
        child = subprocess.run([sys.executable, '-c', code, str(studio.root), job['id']],
                               capture_output=True, text=True, check=True)
        assert child.stdout.strip() == 'False'
    finally:
        job_locks.release(studio.root, job['id'])
    child = subprocess.run([sys.executable, '-c', code, str(studio.root), job['id']],
                           capture_output=True, text=True, check=True)
    assert child.stdout.strip() == 'True'


def test_idempotency_keys_and_exports_survive_a_restart(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    body = {'base_revision': revision['id'], 'timeline': timeline, 'request_id': 'durable'}
    first = studio.client.put(f'{API}/clips/{short["id"]}/timeline', json=body).json()
    exported = studio.wait(export(studio, short, first['revision_id']).json()['job_id'])
    studio_api._STORES.clear()
    assert studio.client.put(f'{API}/clips/{short["id"]}/timeline', json=body).json() == first
    assert studio.client.get(exported['result']['video_url']).status_code == 200


def test_files_route_rejects_unknown_ids_traversal_and_escaping_records(studio, tmp_path):
    secret = tmp_path / 'secret.mp4'
    secret.write_bytes(b'secret')
    inside = studio.root / 'collection-artifacts' / 'studio' / 'x'
    inside.mkdir(parents=True)
    legit = inside / 'a.mp4'
    legit.write_bytes(b'ok-video')
    escaping = studio_api.studio().add_export({'id': 'escape', 'clip_id': 'c', 'revision_id': 'r',
                                               'video_path': str(secret), 'srt_path': str(secret)})
    traversal = studio_api.studio().add_export({'id': 'dots', 'clip_id': 'c', 'revision_id': 'r',
                                                'video_path': str(inside / '..' / '..' / '..' / '..' / 'secret.mp4'), 'srt_path': str(legit)})
    studio_api.studio().add_export({'id': 'fine', 'clip_id': 'c', 'revision_id': 'r', 'video_path': str(legit), 'srt_path': str(legit)})
    assert studio.client.get(f'{API}/files/fine/video').content == b'ok-video'
    for artifact in ('escape', 'dots', 'unknown', '..%2f..%2fsecret', 'a%2fb', 'x' * 200):
        assert studio.client.get(f'{API}/files/{artifact}/video').status_code == 404, artifact
    assert studio.client.get(f'{API}/files/dots/subtitles').status_code == 200  # only the escaping path is refused
    assert studio.client.get(f'{API}/files/fine/manifest').status_code == 422
    assert escaping['id'] == 'escape' and traversal['id'] == 'dots'
    legit.unlink()
    assert studio.client.get(f'{API}/files/fine/video').status_code == 404


def test_older_preview_artifacts_serve_through_the_same_route_and_become_the_preview(studio):
    _, short, revision = studio.clip()
    folder = studio.root / 'collection-artifacts' / revision['id'] / 'preview-t-l'
    folder.mkdir(parents=True)
    (folder / 'short.mp4').write_bytes(b'legacy-preview-bytes')
    (folder / 'short.srt').write_text('1\n')
    with store()._transaction() as db:
        store()._save(db, 'artifact', {'id': 'legacy-art', 'short_id': short['id'], 'collection_id': short['collection_id'],
                                       'revision_id': revision['id'], 'kind': 'preview',
                                       'document': {'video_path': str(folder / 'short.mp4'), 'srt_path': str(folder / 'short.srt')}}, short['id'])
    assert studio.detail(short['id'])['clip']['preview_url'] == f'{API}/files/legacy-art/video'
    assert studio.client.get(f'{API}/files/legacy-art/video', headers={'Range': 'bytes=0-5'}).status_code == 206
    # A studio edit makes that preview stale: it belongs to the previous revision.
    timeline = studio.detail(short['id'])['timeline']
    studio.client.put(f'{API}/clips/{short["id"]}/timeline', json={'base_revision': revision['id'], 'timeline': timeline, 'request_id': 'n'})
    assert studio.detail(short['id'])['clip']['preview_url'] is None


def test_failed_jobs_leave_activity_after_a_day_but_recent_ones_stay(studio):
    now = 1_800_000_000.0
    job = {'id': 'j' * 32, 'kind': 'export', 'status': 'failed', 'error': 'Render failed', 'progress': 0.1,
           'clip_id': None, 'updated_at': now - 2 * 3600}
    assert studio_api.studio_job_activity(job, now)['state'] == 'attention'
    assert studio_api.studio_job_activity({**job, 'updated_at': now - 25 * 3600}, now) is None
