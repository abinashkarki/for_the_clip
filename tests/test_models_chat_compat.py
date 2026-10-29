"""Chat-completions providers (NVIDIA, OpenRouter, Groq, local), model lists, presets and key checks."""
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from clippie import app
from clippie.models import ModelError, ModelRequest, catalog, gateway
from clippie.models.providers import chat_compat
from tests.support_models import install, slow, sse, stream_response

SCHEMA = {'type': 'object', 'properties': {'pick': {'type': 'string'}, 'note': {'type': 'string'}},
          'required': ['pick'], 'additionalProperties': False}
KEYS = {'nvidia': 'nv-secret', 'openrouter': 'or-secret', 'groq': 'gq-secret'}


def chat_stream(text='{"pick":"u1"}', finish='stop', usage=True, model='served-model'):
    half = len(text) // 2
    events = [{'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': text[:half]}}]},
              {'model': model, 'choices': [{'index': 0, 'delta': {'content': text[half:]}, 'finish_reason': finish}]}]
    if usage:
        events.append({'model': model, 'choices': [], 'usage': {
            'prompt_tokens': 1200, 'completion_tokens': 90, 'total_tokens': 1290,
            'completion_tokens_details': {'reasoning_tokens': 40}, 'prompt_tokens_details': {'cached_tokens': 200}}})
    return stream_response(': OPENROUTER PROCESSING\n\n' + sse(*events) + 'data: [DONE]\n\n')


def request(**kw):
    values = dict(stage='review', prompt='Pick the best excerpt.', json_schema=SCHEMA, provider='nvidia',
                  model='nvidia/nemotron-3-super-120b-a12b', system='You are an editor.')
    values.update(kw)
    return ModelRequest(**values)


@pytest.mark.parametrize('provider,host', [('nvidia', 'integrate.api.nvidia.com'), ('openrouter', 'openrouter.ai'),
                                           ('groq', 'api.groq.com')])
def test_each_endpoint_streams_validates_and_logs(monkeypatch, provider, host):
    seen = []

    def handler(req):
        seen.append(req)
        return chat_stream()
    install(monkeypatch, handler)
    result = gateway.complete(request(provider=provider, model='m-1'), KEYS)
    assert result.json == {'pick': 'u1'} and result.provider == provider and result.model_version == 'served-model'
    assert result.usage['input_tokens'] == 1200 and result.usage['output_tokens'] == 50
    assert result.usage['thinking_tokens'] == 40 and result.usage['cached_tokens'] == 200
    sent = seen[0]
    assert sent.url.host == host and sent.url.path.endswith('/chat/completions')
    assert sent.headers['authorization'] == f'Bearer {KEYS[provider]}'
    body = json.loads(sent.content)
    assert body['stream'] is True and body['stream_options'] == {'include_usage': True}
    assert body['response_format']['type'] == 'json_schema' and body['response_format']['json_schema']['strict'] is False
    assert body['messages'][0]['role'] == 'system' and '"pick"' in body['messages'][0]['content']
    assert body['messages'][1] == {'role': 'user', 'content': 'Pick the best excerpt.'}
    row = gateway.get_call(result.call_id)
    assert row['provider'] == provider and row['status'] == 'ok'
    assert KEYS[provider] not in json.dumps(row)


def test_code_fenced_json_is_accepted(monkeypatch):
    install(monkeypatch, lambda req: chat_stream('```json\n{"pick":"u2"}\n```'))
    assert gateway.complete(request(), KEYS).json == {'pick': 'u2'}


def test_images_use_chat_content_parts(monkeypatch):
    seen = []
    install(monkeypatch, lambda req: seen.append(json.loads(req.content)) or chat_stream())
    gateway.complete(request(provider='openrouter', model='google/gemma-4-31b-it:free',
                             images=[{'mime': 'image/jpeg', 'data': b'\xff\xd8jpg', 'label': 'Frame 1'}]), KEYS)
    parts = seen[0]['messages'][1]['content']
    assert parts[0] == {'type': 'text', 'text': 'Frame 1'}
    assert parts[1]['type'] == 'image_url' and parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert parts[-1] == {'type': 'text', 'text': 'Pick the best excerpt.'}


def test_schema_mismatch_and_length_and_status_errors(monkeypatch):
    install(monkeypatch, lambda req: chat_stream('{"note":"no pick"}'))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'invalid_output'
    install(monkeypatch, lambda req: chat_stream('{"pick":', finish='length'))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'output_limit'
    install(monkeypatch, lambda req: httpx.Response(429, headers={'retry-after': '7'}, json={'error': 'slow down'}))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'quota' and 'slow down' not in str(error.value)
    install(monkeypatch, lambda req: httpx.Response(401, json={}))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider='openrouter', model='x'), KEYS)
    assert error.value.category == 'auth' and 'OpenRouter' in str(error.value)


def test_mid_stream_error_event(monkeypatch):
    install(monkeypatch, lambda req: stream_response(sse({'error': {'code': 429, 'message': 'rate'}})))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider='openrouter', model='x'), KEYS)
    assert error.value.category == 'quota'


@pytest.mark.parametrize('code,message,category', [
    (400, 'Unsupported image input', 'config'),
    ('401', 'Invalid credential', 'auth'),
    (None, 'ValueError: Received multimodal data but multimodal processing is not enabled.', 'config'),
    (503, 'Temporarily unavailable', 'service'),
])
def test_stream_errors_keep_configuration_and_auth_failures_distinct(monkeypatch, code, message, category):
    install(monkeypatch, lambda req: stream_response(sse({'error': {'code': code, 'message': message}})))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == category
    assert error.value.detail == message


def test_stream_error_details_redact_credentials(monkeypatch):
    message = 'Rejected key AIzaSyA1234567890abcdefghijkl'
    install(monkeypatch, lambda req: stream_response(sse({'error': {'code': 400, 'message': message}})))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert '[redacted]' in error.value.detail and 'AIza' not in error.value.detail


def test_idle_timeout_on_stalled_stream(monkeypatch):
    install(monkeypatch, lambda req: slow([sse({'choices': [{'delta': {'content': '{'}}]})], 0.6))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(idle_timeout=0.2), KEYS)
    assert error.value.category == 'timeout'


def test_missing_key_is_auth_but_local_server_needs_none(monkeypatch):
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider='nvidia'), {})
    assert error.value.category == 'auth' and 'NVIDIA NIM' in str(error.value)
    seen = []
    install(monkeypatch, lambda req: seen.append(req) or chat_stream())
    monkeypatch.setenv('CLIPPIE_LOCAL_LLM_URL', 'http://127.0.0.1:1234/v1')
    result = gateway.complete(request(provider='ollama', model='qwen3:30b'), {})
    assert seen[0].url.port == 1234 and 'authorization' not in seen[0].headers
    assert result.cost_usd == 0.0


def test_cost_free_openrouter_models_are_zero_others_unknown(monkeypatch):
    install(monkeypatch, lambda req: chat_stream())
    assert gateway.complete(request(provider='openrouter', model='google/gemma-4-31b-it:free'), KEYS).cost_usd == 0.0
    assert gateway.complete(request(provider='nvidia', model='nvidia/unknown'), KEYS).cost_usd is None


def test_missing_keys_follow_routing(monkeypatch):
    monkeypatch.setattr(gateway, 'routing', lambda: {'defaults': {s: {'provider': 'openrouter', 'model': 'm:free'}
                                                                  for s in gateway.CLOUD_STAGES}, 'fallbacks': {}})
    assert gateway.missing_keys({'gemini': 'g'}) == ['OpenRouter']
    assert gateway.keys_message({'openrouter': 'k'}) is None
    assert 'OpenRouter' in gateway.keys_message({})


def test_model_lists_per_provider():
    def handler(req):
        if req.url.host == 'generativelanguage.googleapis.com':
            assert req.headers['x-goog-api-key'] == 'g'
            return httpx.Response(200, json={'models': [
                {'name': 'models/gemini-3.8-flash', 'supportedGenerationMethods': ['generateContent']},
                {'name': 'models/text-embedding', 'supportedGenerationMethods': ['embedContent']}]})
        if req.url.host == 'openrouter.ai':
            return httpx.Response(200, json={'data': [{'id': 'paid/model'}, {'id': 'google/gemma-4-31b-it:free'}]})
        if req.url.host == 'integrate.api.nvidia.com':
            assert req.url.path == '/v1/models'
            return httpx.Response(401, json={})
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    assert catalog.available('gemini', 'g', transport=transport) == ['gemini-3.8-flash']
    assert catalog.available('openrouter', 'k', transport=transport) == ['google/gemma-4-31b-it:free', 'paid/model']
    with pytest.raises(catalog.ListError, match='rejected'):
        catalog.available('nvidia', 'bad', transport=transport)


def test_presets_resolve_against_live_lists():
    routing, notes = catalog.resolve_preset('gemini_free', {'gemini': ['gemini-2.5-flash-lite', 'gemini-3.8-flash']})
    assert routing['defaults']['map'] == {'provider': 'gemini', 'model': 'gemini-2.5-flash-lite'}
    assert routing['defaults']['review']['model'] == 'gemini-3.8-flash' and not notes
    routing, notes = catalog.resolve_preset('nvidia_free', {'nvidia': ['something/else']})
    assert routing['defaults']['map']['model'] == 'nvidia/nemotron-3-super-120b-a12b' and notes
    routing, _ = catalog.resolve_preset('openrouter_free', {'openrouter': None})
    assert all(r['provider'] == 'openrouter' for r in routing['defaults'].values())
    for preset in catalog.presets():
        resolved, _ = catalog.resolve_preset(preset['id'], {})
        gateway.save_routing(resolved)  # every preset is valid routing


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, 'KEYS', {'deepseek': '', 'groq': '', 'gemini': '', 'anthropic': '', 'openai': '',
                                      'nvidia': '', 'openrouter': ''})
    return TestClient(app.app)


def test_api_keys_presets_and_available(client, monkeypatch):
    saved = client.post('/api/settings', json={'nvidia_key': 'nv-private', 'openrouter_key': 'or-private'}).json()
    assert saved['nvidia_configured'] and saved['openrouter_configured'] and 'nv-private' not in json.dumps(saved)
    assert {p['id'] for p in client.get('/api/models/presets').json()['presets']} >= {'gemini_free', 'nvidia_free', 'openrouter_free'}
    assert client.post('/api/models/routing/preset/gemini_free').status_code == 422  # no Gemini key
    monkeypatch.setattr(catalog, 'available', lambda provider, key, transport=None: ['google/gemma-4-31b-it:free'])
    applied = client.post('/api/models/routing/preset/openrouter_free')
    assert applied.status_code == 200, applied.text
    assert client.get('/api/models/routing').json()['defaults']['map']['provider'] == 'openrouter'
    assert client.get('/api/models/available/openrouter').json()['models'] == ['google/gemma-4-31b-it:free']
    assert client.get('/api/models/available/gemini').status_code == 422
    assert client.get('/api/models/available/nope').status_code == 404
    assert client.post('/api/models/routing/preset/nope').status_code == 404


def test_provider_error_detail_is_logged_without_secrets(monkeypatch):
    body = {'error': {'code': 400, 'message': 'The specified schema produces a constraint that has too many states. key=AIzaSyA1234567890abcdefghijkl'}}
    install(monkeypatch, lambda req: httpx.Response(400, json=body))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider='gemini', model='gemini-test'), {'gemini': 'g'})
    assert 'too many states' in error.value.detail and 'AIza' not in error.value.detail
    row = gateway.calls(1)[0]
    detail = gateway.get_call(row['call_id'])['response']['error']['provider_detail']
    assert 'too many states' in detail and '[redacted]' in detail


def test_replay_endpoint(client, monkeypatch):
    install(monkeypatch, lambda req: chat_stream())
    first = gateway.complete(request(provider='openrouter', model='m:free', json_schema=None), KEYS)
    monkeypatch.setattr(app, 'KEYS', {**app.KEYS, **KEYS})
    out = client.post(f'/api/models/calls/{first.call_id}/replay', json={'provider': 'nvidia', 'model': 'x/y'}).json()
    assert out['ok'] and out['model'] == 'x/y' and gateway.get_call(out['call_id'])['replay_of'] == first.call_id
    install(monkeypatch, lambda req: httpx.Response(404, json={'error': {'message': 'model not found'}}))
    out = client.post(f'/api/models/calls/{first.call_id}/replay', json={}).json()
    assert out['ok'] is False and out['provider_detail'] == 'model not found'
    assert client.post('/api/models/calls/nope/replay', json={}).status_code == 404


def test_gemini_gets_a_relaxed_schema_but_property_names_survive():
    from clippie.models.providers.gemini import provider_schema
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['minimum'],
              'properties': {'minimum': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                             'items': {'type': 'array', 'maxItems': 30, 'items': {'type': 'string', 'maxLength': 9}},
                             'kind': {'type': 'string', 'enum': ['a', 'b']}}}
    assert provider_schema(schema) == {'type': 'object', 'required': ['minimum'], 'properties': {
        'minimum': {'type': 'integer'}, 'items': {'type': 'array', 'items': {'type': 'string'}},
        'kind': {'type': 'string', 'enum': ['a', 'b']}}}


def test_overlong_notes_are_trimmed_not_failed(monkeypatch):
    schema = {'type': 'object', 'required': ['issues', 'status'], 'properties': {
        'status': {'type': 'string', 'enum': ['pass', 'needs_revision']},
        'issues': {'type': 'array', 'maxItems': 2, 'items': {'type': 'string', 'maxLength': 40}}}}
    long = 'Drops qualification: speech says potentially, the hook states it as fact.'
    install(monkeypatch, lambda req: chat_stream(json.dumps({'status': 'needs_revision', 'issues': [long, 'b', 'c']})))
    value = gateway.complete(request(json_schema=schema), KEYS).json
    assert len(value['issues']) == 2 and len(value['issues'][0]) <= 40 and value['issues'][0].endswith('…')
    install(monkeypatch, lambda req: chat_stream(json.dumps({'status': 'maybe', 'issues': []})))
    with pytest.raises(ModelError):
        gateway.complete(request(json_schema=schema), KEYS)


def test_discovery_sees_the_primary_transcript():
    from clippie.collection_editor import _transcript
    artifacts = {'src': {'units': [{'id': 'u-1', 'start': 0.0, 'text': 'Hello there.'},
                                   {'id': 'u-2', 'start': 3.25, 'text': 'Second line.'}]},
                 'pic': {'units': [{'id': 'p-1', 'start': 0.0, 'text': 'Picture only.'}]}}
    assert _transcript(artifacts, {'source_roles': {'pic': 'picture'}}) == [['u-1', 0.0, 'Hello there.'],
                                                                            ['u-2', 3.2, 'Second line.']]



def test_rate_limits_release_worker_and_share_cooldown(monkeypatch):
    seen = []
    install(monkeypatch, lambda req: seen.append(req) or httpx.Response(429, headers={'retry-after': '3'}, json={}))
    pauses = []
    monkeypatch.setattr(gateway, 'SLEEP', pauses.append)
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'quota' and error.value.attempts == 1
    with pytest.raises(ModelError) as cooling:
        gateway.complete(request(), KEYS)
    assert cooling.value.category == 'quota' and cooling.value.attempts == 0
    assert len(seen) == 1 and pauses == []


def test_route_options_reach_the_request_body_and_are_validated(monkeypatch):
    seen = []
    install(monkeypatch, lambda req: seen.append(json.loads(req.content)) or chat_stream())
    routing = {'defaults': {'review': {'provider': 'nvidia', 'model': 'z-ai/glm-5.3',
                                       'options': {'chat_template_kwargs': {'enable_thinking': False}}}}, 'fallbacks': {}}
    gateway.save_routing(routing)
    gateway.complete(request(provider=None, model=None), KEYS)
    assert seen[0]['chat_template_kwargs'] == {'enable_thinking': False} and seen[0]['model'] == 'z-ai/glm-5.3'
    with pytest.raises(ValueError):
        gateway.save_routing({'defaults': {'review': {'provider': 'nvidia', 'model': 'x', 'options': {'model': 'y'}}},
                              'fallbacks': {}})


def test_reasoning_only_stream_records_partial_progress(monkeypatch):
    thinking = sse(*[{'choices': [{'delta': {'reasoning_content': 'thinking... '}}]} for _ in range(5)])
    install(monkeypatch, lambda req: slow([thinking, thinking], 0.15))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(total_timeout=0.25, idle_timeout=0.2), KEYS)
    assert error.value.category == 'timeout'
    assert error.value.partial['reasoning_chars'] >= 60 and error.value.partial['text_chars'] == 0
    row = gateway.get_call(error.value.call_id)
    assert row['response']['error']['partial']['reasoning_chars'] >= 60 and row['ttft_ms'] is not None


def test_truncated_slate_keeps_complete_candidates(monkeypatch):
    schema = {'type': 'object', 'required': ['candidates', 'rejected', 'explanation'], 'properties': {
        'candidates': {'type': 'array', 'items': {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'string'}}}},
        'rejected': {'type': 'array', 'items': {'type': 'object'}}, 'explanation': {'type': 'string'}}}
    cut = '{"candidates": [{"id": "a", "note": "brace } in \\"text\\""}, {"id": "b"}, {"id": "c", "no'
    install(monkeypatch, lambda req: chat_stream(cut, finish='length'))
    result = gateway.complete(request(json_schema=schema), KEYS)
    assert [c['id'] for c in result.json['candidates']] == ['a', 'b'] and result.json['rejected'] == []
    assert result.finish_reason == 'length_salvaged'
    install(monkeypatch, lambda req: chat_stream('{"candidates": [{"id": "a", "no', finish='length'))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(json_schema=schema), KEYS)
    assert error.value.category == 'output_limit'
