from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

from clippie import app as module
from clippie.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'STORE', Store(tmp_path / 'data'))
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(module, 'KEYS', {'deepseek': '', 'groq': ''})
    with TestClient(module.app) as client:
        yield client
    module.WORKER.shutdown(wait=True)


def wait_job(client, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f'/api/jobs/{job_id}').json()
        if job['status'] in {'ready', 'failed'}:
            assert job['status'] == 'ready', job.get('error')
            return job
        time.sleep(.03)
    pytest.fail('Timed out waiting for job')


def test_source_intake_transcribes_and_serves_preview(client, tmp_path):
    source = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=24:d=14', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=14', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True)
    words = [{'id': i, 'text': text, 'start': i * 1., 'end': i + .8} for i, text in enumerate('Better decisions start here. Test an idea first. Then learn from reality.'.split())]
    transcript = tmp_path / 'words.json'
    transcript.write_text(json.dumps({'words': words}))
    response = client.post('/api/jobs', json={'source': str(source), 'transcript_path': str(transcript), 'transcriber': 'import', 'brief': 'better decisions', 'min_duration': 3, 'max_duration': 8, 'max_clips': 2})
    assert response.status_code == 202
    job_id = response.json()['id']
    job = wait_job(client, job_id)
    assert job['words'] == words
    assert job['clips']
    assert len(job['runs']) == 1
    assert client.get(job['media_url'], headers={'Range': 'bytes=0-63'}).status_code == 206
    assert Store(module.STORE.root).get(job_id)['words'] == words


def test_studio_is_served_at_root_and_legacy_pages_are_gone(client):
    for path in ('/', '/studio'):
        page = client.get(path)
        assert page.status_code == 200 and '<div id="root">' in page.text
    assert client.get('/').headers['cache-control'] == 'no-store'
    for path in ('/collections', '/static/app.js', '/api/productions', '/api/demo'):
        assert client.get(path).status_code in {404, 405}


def test_credentials_are_not_echoed_and_origin_is_checked(client):
    result = client.post('/api/settings', json={'deepseek_key': 'a-test-secret'})
    assert result.json()['deepseek_configured']
    assert 'a-test-secret' not in result.text
    assert 'a-test-secret' not in client.get('/api/jobs').text
    assert client.post('/api/settings', json={'deepseek_key': 'bad'}, headers={'Origin': 'https://example.com'}).status_code == 403
    assert client.get('/api/health', headers={'Host': 'evil.com'}).status_code == 403


def test_invalid_setup_fails_before_enqueuing(client):
    assert client.post('/api/jobs', json={'source': '/missing/video.mp4'}).status_code == 422
    result = client.post('/api/jobs', json={'source': 'https://example.com/video', 'selector': 'deepseek'})
    assert result.status_code == 422
    assert 'key' in result.json()['detail']
    assert client.get('/api/jobs').json() == []


def test_interrupted_work_is_marked_failed(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save({'id': 'interrupted', 'status': 'transcribing'})
    monkeypatch.setattr(module, 'STORE', store)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    with TestClient(module.app):
        assert store.get('interrupted')['status'] == 'failed'


def test_health_reports_local_models_with_prepare_commands(client):
    health = client.get('/api/health').json()
    assert set(health['local_models']) == {'whisper', 'embeddings', 'vision', 'editor'}
    for item in health['local_models'].values():
        assert item['command'].startswith('uv run ') and 'scripts/prepare_' in item['command']
        assert isinstance(item['ready'], bool) and isinstance(item['installed'], bool)
