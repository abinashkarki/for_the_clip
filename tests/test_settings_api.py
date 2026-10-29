from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from clippie import app
from clippie.credentials import load_keys
from clippie.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'STORE', Store(tmp_path/'data'))
    monkeypatch.setattr(app, 'WORKER', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(app, 'KEYS', {'deepseek': '', 'groq': '', 'gemini': ''})
    with TestClient(app.app) as test:
        yield test
    app.WORKER.shutdown(wait=True)


def test_private_env_is_literal_allowlisted_and_environment_takes_priority(tmp_path, monkeypatch):
    (tmp_path/'.env').write_text('GEMINI_API_KEY="literal-$(never-execute)"\nUNRELATED=value\nGROQ_API_KEY=groq-test\n')
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    assert load_keys(tmp_path)['gemini'] == 'literal-$(never-execute)'
    monkeypatch.setenv('GEMINI_API_KEY', 'environment-value')
    assert load_keys(tmp_path)['gemini'] == 'environment-value'


def test_saved_key_can_be_removed_without_echo_and_invalid_inputs_are_redacted(client):
    saved = client.post('/api/settings', json={'gemini_key': 'private-replaceable-key'})
    assert saved.status_code == 200
    assert saved.json()['credential_storage']['gemini'] == 'keychain'
    assert client.delete('/api/settings/gemini').json()['gemini_configured'] is False
    assert client.delete('/api/settings/not-a-provider').status_code == 404
    secret = 'private-key-'*500
    invalid = client.post('/api/settings', json={'gemini_key': secret})
    assert invalid.status_code == 422 and secret not in invalid.text
    unknown = client.post('/api/settings', json={'unsupported_secret': 'never-echo-this'})
    assert unknown.status_code == 422 and 'never-echo-this' not in unknown.text


def test_failed_persistence_keeps_existing_key_and_returns_safe_error(client, monkeypatch):
    from clippie import credentials
    client.post('/api/settings', json={'gemini_key': 'existing-test-key'})
    def broken(): raise RuntimeError('sensitive-backend-message')
    monkeypatch.setattr(credentials, 'keychain', broken)
    response = client.post('/api/settings', json={'gemini_key': 'new-test-secret'})
    assert response.status_code == 503
    assert app.KEYS['gemini'] == 'existing-test-key'
    assert 'new-test-secret' not in response.text and 'sensitive-backend' not in response.text
