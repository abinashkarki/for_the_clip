"""Studio API end to end against the real timeline, filler, template and render modules (no stand-ins)."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from clippie import app as module, download, intake, studio_api
from clippie.store import Store
from tests.test_studio_support import WORDS, Studio, make_video, v2_timeline


@pytest.fixture(scope='session')
def hd_video_bytes(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp('studio-hd') / 'source.mp4', size='1920x1080').read_bytes()


@pytest.fixture
def real_studio(tmp_path, monkeypatch, hd_video_bytes):
    media_path = tmp_path / 'source.mp4'
    media_path.write_bytes(hd_video_bytes)
    sources = Store(tmp_path / 'data')
    sources.save({'id': 'src', 'title': 'Integration source', 'status': 'ready', 'progress': 'Ready',
                  'source_path': str(media_path), 'preview_path': str(media_path), 'duration': 10.0,
                  'source_info': {'width': 1920, 'height': 1080, 'duration': 10.0, 'has_audio': True},
                  'words': [{'id': i, 'text': t, 'start': a, 'end': b} for i, t, a, b in WORDS], 'segments': []})
    monkeypatch.setattr(module, 'STORE', sources)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(module, 'KEYS', {'gemini': 'fake-test-key', 'deepseek': '', 'groq': ''})
    monkeypatch.setattr(module, 'process_job', lambda job_id, request: None)
    with TestClient(module.app, headers={'X-Clippie-Observation': 'qa'}) as client:
        yield Studio(client, tmp_path / 'data', media_path, None)
    module.WORKER.shutdown(wait=True)


def _probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                         check=True, capture_output=True, text=True).stdout
    data = json.loads(out)
    video = next(s for s in data['streams'] if s['codec_type'] == 'video')
    return float(data['format']['duration']), int(video['width']), int(video['height']), \
        any(s['codec_type'] == 'audio' for s in data['streams'])


def test_v2_clip_edits_cuts_fillers_and_exports_with_real_engine(real_studio):
    studio = real_studio
    _, short, revision = studio.clip()
    detail = studio.detail(short['id'])
    timeline = detail['timeline']
    assert timeline['schema_version'] == 3 and timeline['captions']['template_id'] == 'boxed_word'
    assert [w['text'] for w in detail['words']][:3] == ['So', 'how', 'um']

    timeline['captions']['template_id'] = 'bold_pop'
    timeline['overlays'] = [{'id': 'h1', 'kind': 'hook', 'text': 'One question changes tutoring', 'start': 0, 'end': 2,
                             'style': 'hook_box'},
                            {'id': 'p1', 'kind': 'progress_bar', 'color': '#FFE600', 'position': 'bottom', 'thickness': 12}]
    saved = studio.client.put(f"/api/studio/clips/{short['id']}/timeline",
                              json={'base_revision': revision['id'], 'timeline': timeline, 'request_id': 'edit-1'})
    assert saved.status_code == 200, saved.text
    base = saved.json()['revision_id']

    trimmed = studio.client.post(f"/api/studio/clips/{short['id']}/actions/remove-fillers",
                                 json={'base_revision': base, 'request_id': 'fill-1', 'silence_min': 0.6})
    assert trimmed.status_code == 200, trimmed.text
    body = trimmed.json()
    cuts = [c for s in body['timeline']['segments'] for c in s['cuts']]
    assert cuts, 'expected filler/silence cuts from the real detector'
    after = studio.detail(short['id'])
    removed = {w['text'] for w in after['words'] if w['removed']}
    assert {'um', 'uh'} <= removed and 'problem' not in removed

    job = studio.client.post(f"/api/studio/clips/{short['id']}/export",
                             json={'revision_id': body['revision_id'], 'quality': 'preview'})
    assert job.status_code == 202, job.text
    result = studio.wait(job.json()['job_id'], timeout=180)
    assert result['status'] == 'done', result
    video = studio.client.get(result['result']['video_url'])
    assert video.status_code == 200 and len(video.content) > 1000
    out = studio.root / 'check.mp4'
    out.write_bytes(video.content)
    duration, width, height, audio = _probe(out)
    assert (width, height) == (540, 960) and audio
    assert duration < 8.0 - 0.5  # cuts shortened the 8 s clip


def test_saved_master_exports_with_real_renderer_without_redownload(real_studio, monkeypatch):
    studio = real_studio
    module.STORE.update('src', source='https://example.test/video',
                        provenance={'id': 'video', **intake.master_provenance(studio.media)})

    def no_download(*args, **kwargs):
        raise AssertionError('A saved master must never trigger an online download')

    monkeypatch.setattr(download, 'download_file', no_download)
    _, short, revision = studio.clip()
    response = studio.client.post(f'/api/studio/clips/{short["id"]}/export',
                                  json={'revision_id': revision['id'], 'quality': '1080p'})
    assert response.status_code == 202, response.text
    result = studio.wait(response.json()['job_id'], timeout=180)
    assert result['status'] == 'done', result
    artifact_id = result['result']['video_url'].split('/')[-2]
    artifact = studio_api.studio().get_export(artifact_id)
    assert artifact['final_quality']['sources']['src']['file'] == str(studio.media)
    duration, width, height, audio = _probe(artifact['video_path'])
    assert (width, height) == (1080, 1920) and audio and duration > 0
    # Simulate a crash after the export record was committed but before the job was completed.
    from clippie import job_locks
    studio_api.studio().update_job(response.json()['job_id'], status='running', result=None)
    job_locks.release(studio.root, response.json()['job_id'])
    studio_api.recover_jobs()
    recovered = studio_api.studio().get_job(response.json()['job_id'])
    assert recovered['status'] == 'done' and artifact_id in recovered['result']['video_url']


def test_real_export_recovers_after_owner_process_dies(real_studio):
    from clippie import job_locks
    studio = real_studio
    _, short, revision = studio.clip()
    _, collection = studio_api.clip_context(short['id'])
    sources = studio_api.revision_sources(collection, revision)
    timeline = studio_api.engine.validate(studio_api.timeline_for(revision, collection, sources), sources)
    assets = studio_api.asset_paths(timeline)
    job = studio_api.studio().create_job('export', short['id'], {
        'revision_id': revision['id'], 'quality': '1080p',
        'input_fingerprint': studio_api.export_input_fingerprint(timeline, sources, assets)})
    job_locks.release(studio.root, job['id'])
    code = ('from clippie import job_locks; import sys,time; '
            'print(job_locks.acquire(sys.argv[1],sys.argv[2]),flush=True); time.sleep(60)')
    owner = subprocess.Popen([sys.executable, '-c', code, str(studio.root), job['id']], stdout=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == 'True'
        studio_api.studio().update_job(job['id'], status='running')
        studio_api.recover_jobs()
        assert studio_api.studio().get_job(job['id'])['status'] == 'running'
    finally:
        owner.kill()
        owner.wait(timeout=5)
        owner.stdout.close()
    studio_api.recover_jobs()
    result = studio.wait(job['id'], timeout=180)
    assert result['status'] == 'done', result
    assert studio_api.studio().get_job(job['id'])['attempt'] == 2
