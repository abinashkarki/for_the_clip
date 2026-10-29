import json

from fastapi.testclient import TestClient
import pytest

from clippie import app
from clippie.models import ModelRequest, gateway
from tests.support_models import gemini_stream, install

KEYS = {'gemini': 'g-secret'}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, 'KEYS', {'deepseek': '', 'groq': '', 'gemini': '', 'anthropic': '', 'openai': ''})
    install(monkeypatch, lambda request: gemini_stream())
    return TestClient(app.app)


def make_call(stage='plan'):
    request = ModelRequest(stage=stage, prompt='hello', json_schema={'type': 'object'}, provider='gemini',
                           model='gemini-test', metadata={'collection_id': 'c9'})
    return gateway.complete(request, KEYS)


def test_calls_list_filter_and_detail(client):
    first, second = make_call('plan'), make_call('map')
    listed = client.get('/api/models/calls').json()['calls']
    assert [row['call_id'] for row in listed] == [second.call_id, first.call_id]
    assert 'request_blob' not in listed[0] and 'request' not in listed[0]
    assert [r['stage'] for r in client.get('/api/models/calls?stage=map').json()['calls']] == ['map']
    assert client.get('/api/models/calls?provider=openai').json()['calls'] == []
    assert len(client.get('/api/models/calls?limit=1').json()['calls']) == 1
    detail = client.get(f'/api/models/calls/{first.call_id}').json()
    assert detail['request']['prompt'] == 'hello' and detail['response']['text'] == '{"ok":true}'
    assert 'g-secret' not in json.dumps(detail)
    assert client.get('/api/models/calls/nope').status_code == 404
    assert client.get('/api/models/calls?limit=0').status_code == 422


def test_summary(client):
    make_call(); make_call()
    body = client.get('/api/models/summary').json()
    assert body['prices_as_of'] and body['groups'][0]['calls'] == 2
    assert body['groups'][0]['cost_usd'] is None and body['groups'][0]['cost_unknown_calls'] == 2


def test_routing_get_put(client):
    assert client.get('/api/models/routing').json()['defaults']['plan']['provider'] == 'gemini'
    update = {'defaults': {'plan': {'provider': 'openai', 'model': 'gpt-5-mini'}}}
    saved = client.put('/api/models/routing', json=update)
    assert saved.status_code == 200 and saved.json()['defaults']['plan']['model'] == 'gpt-5-mini'
    assert client.get('/api/models/routing').json()['defaults']['plan']['provider'] == 'openai'
    assert client.get('/api/models/routing').json()['defaults']['map']['provider'] == 'gemini'
    bad = client.put('/api/models/routing', json={'defaults': {'plan': {'provider': 'acme', 'model': 'x'}}})
    assert bad.status_code == 422


def test_settings_store_anthropic_and_openai_keys(client):
    before = client.get('/api/settings').json()
    assert before['anthropic_configured'] is False and before['openai_configured'] is False
    saved = client.post('/api/settings', json={'anthropic_key': 'a-private', 'openai_key': 'o-private'})
    assert saved.status_code == 200
    body = saved.json()
    assert body['anthropic_configured'] and body['openai_configured'] and not body['gemini_configured']
    assert body['credential_storage']['anthropic'] == 'keychain'
    assert 'a-private' not in saved.text and 'o-private' not in saved.text
    health = client.get('/api/health').json()
    assert health['anthropic_configured'] is True and health['openai_configured'] is True
    assert client.delete('/api/settings/anthropic').json()['anthropic_configured'] is False


def test_env_vars_are_the_highest_precedence_for_new_keys(tmp_path, monkeypatch):
    from clippie.credentials import CredentialStore
    (tmp_path/'.env').write_text('ANTHROPIC_API_KEY=from-file\nOPENAI_API_KEY="quoted"\n')
    store = CredentialStore(tmp_path)
    keys = store.load()
    assert keys['anthropic'] == 'from-file' and store.origins['openai'] == 'env_file' and keys['openai'] == 'quoted'
    store.save('openai', 'from-keychain')
    assert store.load()['openai'] == 'from-keychain' and store.origins['openai'] == 'keychain'
    monkeypatch.setenv('OPENAI_API_KEY', 'from-env')
    assert store.load()['openai'] == 'from-env' and store.origins['openai'] == 'environment'


def test_catalog_lists_stages_providers_and_priced_models(client):
    body = client.get('/api/models/catalog').json()
    assert 'plan' in body['stages'] and {p['id'] for p in body['providers']} >= {'gemini', 'anthropic', 'openai', 'local_qwen'}
    assert 'gemini-3.8-flash' in body['models']['gemini']
