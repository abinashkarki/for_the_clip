import gzip
import json
import time

import httpx
import pytest

from clippie.models import ModelError, ModelRequest, gateway
from tests.support_models import (anthropic_stream, gemini_stream, install, openai_stream, slow, sse,
                                  stream_response)

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'],
          'additionalProperties': False}
KEYS = {'gemini': 'g-secret', 'anthropic': 'a-secret', 'openai': 'o-secret'}


def request(**kw):
    values = dict(stage='plan', prompt='Pick the best excerpt.', json_schema=SCHEMA, provider='gemini',
                  model='gemini-test', metadata={'collection_id': 'c1', 'task_id': 't1', 'short_id': 's1'})
    values.update(kw)
    return ModelRequest(**values)


def capture(monkeypatch, response):
    seen = []
    def handler(http_request):
        seen.append(http_request)
        return response() if callable(response) else response
    install(monkeypatch, handler)
    return seen


# Providers -----------------------------------------------------------------

def test_gemini_stream_text_usage_and_request_shape(monkeypatch):
    seen = capture(monkeypatch, gemini_stream())
    result = gateway.complete(request(system='Be careful.'), KEYS)
    assert result.json == {'ok': True} and result.text == '{"ok":true}'
    assert 'secret thinking' not in result.text
    assert result.usage == {'input_tokens': 100, 'output_tokens': 20, 'thinking_tokens': 30,
                            'cached_tokens': 40, 'total_tokens': 150}
    assert result.model_version == 'gemini-test-001' and result.finish_reason == 'stop'
    assert result.ttft_ms is not None and result.latency_ms >= 0
    http = seen[0]
    assert str(http.url).endswith('/models/gemini-test:streamGenerateContent?alt=sse')
    assert http.headers['x-goog-api-key'] == 'g-secret' and 'g-secret' not in str(http.url)
    body = json.loads(http.content)
    assert body['generationConfig']['responseJsonSchema'] == {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
    assert body['generationConfig']['thinkingConfig'] == {'thinkingLevel': 'low'}
    assert body['systemInstruction']['parts'][0]['text'] == 'Be careful.'


def test_anthropic_stream_forced_tool_and_usage(monkeypatch):
    seen = capture(monkeypatch, anthropic_stream())
    result = gateway.complete(request(provider='anthropic', model='claude-test'), KEYS)
    assert result.json == {'ok': True} and result.finish_reason == 'stop'
    assert result.usage['input_tokens'] == 14 and result.usage['cached_tokens'] == 4
    assert result.usage['output_tokens'] == 7 and result.usage['total_tokens'] == 21
    assert result.model_version == 'claude-test-1'
    http = seen[0]
    assert http.headers['x-api-key'] == 'a-secret' and http.headers['anthropic-version']
    body = json.loads(http.content)
    assert body['stream'] is True and body['tool_choice'] == {'type': 'tool', 'name': 'return_json'}
    assert body['tools'][0]['input_schema'] == SCHEMA


def test_anthropic_max_tokens_is_output_limit(monkeypatch):
    capture(monkeypatch, anthropic_stream('{"ok"', stop='max_tokens'))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider='anthropic', model='claude-test'), KEYS)
    assert error.value.category == 'output_limit' and error.value.usage['output_tokens'] == 7


def test_openai_stream_strict_schema_and_usage(monkeypatch):
    seen = capture(monkeypatch, openai_stream())
    nested = {'type': 'object', 'properties': {'a': {'type': 'object', 'properties': {'b': {'type': 'string'}}}}}
    result = gateway.complete(request(provider='openai', model='gpt-test', json_schema=SCHEMA), KEYS)
    assert result.json == {'ok': True}
    assert result.usage == {'input_tokens': 50, 'output_tokens': 20, 'thinking_tokens': 5,
                            'cached_tokens': 10, 'total_tokens': 75}
    assert seen[0].headers['authorization'] == 'Bearer o-secret'
    body = json.loads(seen[0].content)
    assert body['stream'] is True and body['text']['format']['strict'] is True
    from clippie.models.providers import openai
    strict = openai.strict_schema(nested)
    assert strict['additionalProperties'] is False and strict['properties']['a']['required'] == ['b']


def test_openai_incomplete_is_output_limit(monkeypatch):
    capture(monkeypatch, openai_stream('{"o', status='incomplete', incomplete='max_output_tokens'))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider='openai', model='gpt-test'), KEYS)
    assert error.value.category == 'output_limit'


def test_images_are_sent_but_never_logged(monkeypatch):
    seen = capture(monkeypatch, gemini_stream())
    image = {'mime': 'image/jpeg', 'data': b'\xff\xd8RAW-IMAGE-BYTES', 'label': 'Picture p1.'}
    result = gateway.complete(request(images=[image]), KEYS)
    parts = json.loads(seen[0].content)['contents'][0]['parts']
    assert parts[-1]['inlineData']['mimeType'] == 'image/jpeg'
    row = gateway.get_call(result.call_id)
    assert row['request']['images'][0]['sha256'] and row['request']['images'][0]['mime'] == 'image/jpeg'
    assert 'data' not in row['request']['images'][0] and row['image_count'] == 1
    assert b'RAW-IMAGE-BYTES' not in gateway.log().path.read_bytes()
    assert 'UkFXLUlNQUdF' not in json.dumps(row)


def test_json_array_body_without_sse_is_accepted(monkeypatch):
    capture(monkeypatch, httpx.Response(200, json=[{'candidates': [{'content': {'parts': [{'text': '{"ok":true}'}]},
                                                                     'finishReason': 'STOP'}]}]))
    assert gateway.complete(request(), KEYS).json == {'ok': True}


# Timeouts ------------------------------------------------------------------

def stalled(monkeypatch):
    first = sse({'candidates': [{'content': {'parts': [{'text': '{"o'}]}}]})
    install(monkeypatch, lambda r: slow([first, 'data: {}\n\n'], 1.0))


def test_idle_timeout_fires_on_stalled_stream(monkeypatch):
    stalled(monkeypatch)
    started = time.monotonic()
    with pytest.raises(ModelError) as error:
        gateway.complete(request(idle_timeout=0.2, total_timeout=30), KEYS)
    assert error.value.category == 'timeout' and 'idle' in str(error.value)
    assert time.monotonic()-started < 0.9
    assert gateway.get_call(error.value.call_id)['error_category'] == 'timeout'


def test_idle_timeout_resets_on_every_chunk(monkeypatch):
    events = [sse({'candidates': [{'content': {'parts': [{'text': piece}]}}]}) for piece in ['{"o', 'k":', 'tru', 'e}']]
    events.append(sse({'candidates': [{'content': {'parts': [{'text': ''}]}, 'finishReason': 'STOP'}]}))
    install(monkeypatch, lambda r: slow(events, 0.2))
    result = gateway.complete(request(idle_timeout=0.5, total_timeout=30), KEYS)
    assert result.json == {'ok': True} and result.latency_ms >= 900


def test_total_timeout_caps_a_chatty_stream(monkeypatch):
    events = [sse({'candidates': [{'content': {'parts': [{'text': ' '}]}}]})]*40
    install(monkeypatch, lambda r: slow(events, 0.05))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(idle_timeout=1, total_timeout=0.4), KEYS)
    assert error.value.category == 'timeout' and 'total' in str(error.value)


# Errors --------------------------------------------------------------------

@pytest.mark.parametrize('status,category', [(401, 'auth'), (403, 'auth'), (429, 'quota'), (400, 'config'),
                                              (404, 'config'), (500, 'service'), (503, 'service'), (529, 'service')])
@pytest.mark.parametrize('provider', ['gemini', 'anthropic', 'openai'])
def test_http_status_classification_never_echoes_body(monkeypatch, provider, status, category):
    capture(monkeypatch, httpx.Response(status, text='a-secret prompt text echoed', headers={'Retry-After': '3'}))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider=provider, model='m'), KEYS)
    assert error.value.category == category and error.value.status_code == status
    assert error.value.retry_after == 3
    assert 'secret' not in str(error.value) and 'prompt' not in str(error.value)
    row = gateway.get_call(error.value.call_id)
    assert row['status'] == 'error' and row['http_status'] == status and row['cost_usd'] is None


def test_connection_failure_is_network(monkeypatch):
    def refuse(http_request):
        raise httpx.ConnectError('private connection detail')
    install(monkeypatch, refuse)
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'network' and 'private' not in str(error.value)


def test_missing_key_is_auth_and_sends_nothing(monkeypatch):
    seen = capture(monkeypatch, gemini_stream())
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider='anthropic', model='claude-test'), {'gemini': 'x'})
    assert error.value.category == 'auth' and not seen


def test_gemini_max_tokens_is_output_limit(monkeypatch):
    capture(monkeypatch, gemini_stream('{"selections":', finish='MAX_TOKENS'))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'output_limit' and error.value.usage['total_tokens'] == 150


@pytest.mark.parametrize('text', ['not json', '{"ok": "yes"}', '{"ok": true, "extra": 1}', '{}', '[]'])
def test_unparseable_or_schema_invalid_output(monkeypatch, text):
    capture(monkeypatch, gemini_stream(text))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'invalid_output'
    row = gateway.get_call(error.value.call_id)
    assert row['status'] == 'error' and row['response']['text'] == text and row['input_tokens'] == 100


def test_blocked_finish_is_invalid_output(monkeypatch):
    capture(monkeypatch, gemini_stream('{"ok":true}', finish='SAFETY'))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(), KEYS)
    assert error.value.category == 'invalid_output'


def test_no_schema_returns_text_and_best_effort_json(monkeypatch):
    capture(monkeypatch, gemini_stream('plain words'))
    result = gateway.complete(request(json_schema=None), KEYS)
    assert result.text == 'plain words' and result.json is None


# Call log, cost ------------------------------------------------------------

def test_call_log_row_and_blobs_hold_no_secrets(monkeypatch):
    capture(monkeypatch, gemini_stream())
    result = gateway.complete(request(prompt_version='editor-8'), KEYS)
    row = gateway.get_call(result.call_id)
    assert row['provider'] == 'gemini' and row['model'] == 'gemini-test' and row['stage'] == 'plan'
    assert (row['collection_id'], row['task_id'], row['short_id']) == ('c1', 't1', 's1')
    assert row['prompt_version'] == 'editor-8' and row['status'] == 'ok' and row['input_chars'] == len('Pick the best excerpt.')
    assert row['thinking_tokens'] == 30 and row['cached_tokens'] == 40 and row['latency_ms'] is not None
    assert row['request']['prompt'] == 'Pick the best excerpt.' and row['response']['text'] == '{"ok":true}'
    blobs = list((gateway.data_dir()/'model-calls').glob('*/*.json.gz'))
    assert len(blobs) == 2 and all(b.name == b.parent.name + b.name[2:] and b.name.endswith('.json.gz') for b in blobs)
    everything = b''.join(gzip.decompress(b.read_bytes()) for b in blobs) + gateway.log().path.read_bytes()
    assert b'g-secret' not in everything
    assert len(gateway.calls(10, stage='plan')) == 1 and gateway.calls(10, stage='map') == []
    assert 'request_blob' not in gateway.calls(1)[0]


def test_identical_requests_share_a_request_blob(monkeypatch):
    capture(monkeypatch, gemini_stream())
    first = gateway.complete(request(), KEYS)
    second = gateway.complete(request(), KEYS)
    a, b = gateway.get_call(first.call_id), gateway.get_call(second.call_id)
    assert a['request_blob'] == b['request_blob'] and a['input_hash'] == b['input_hash']


def test_cost_from_price_table_and_unknown_is_none(monkeypatch):
    capture(monkeypatch, anthropic_stream())
    known = gateway.complete(request(provider='anthropic', model='claude-sonnet-4-5'), KEYS)
    # 14 input of which 4 cached, 7 output: (10*3 + 4*0.30 + 7*15) / 1e6
    assert known.cost_usd == pytest.approx((10*3 + 4*.30 + 7*15)/1e6)
    unknown = gateway.complete(request(provider='anthropic', model='claude-future-9'), KEYS)
    assert unknown.cost_usd is None
    assert gateway.get_call(unknown.call_id)['cost_usd'] is None


def test_price_file_is_dated_and_override_extends(monkeypatch):
    table = gateway.prices()
    assert table['as_of'] and 'gemini-test' not in table['models']['gemini']
    (gateway.data_dir()/'model-prices.json').write_text(json.dumps(
        {'models': {'gemini': {'gemini-test': {'input': 1, 'output': 2}}}}))
    capture(monkeypatch, gemini_stream())
    result = gateway.complete(request(), KEYS)
    # input 100 (40 cached at the input rate), output 20 + thinking 30
    assert result.cost_usd == pytest.approx((100*1 + 50*2)/1e6)


def test_cost_is_none_when_usage_unknown(monkeypatch):
    capture(monkeypatch, gemini_stream(usage={'totalTokenCount': None}))
    (gateway.data_dir()/'model-prices.json').write_text(json.dumps(
        {'models': {'gemini': {'gemini-test': {'input': 1, 'output': 2}}}}))
    assert gateway.complete(request(), KEYS).cost_usd is None


def test_summary_groups_failures_latency_and_cost(monkeypatch):
    responses = iter([gemini_stream(), gemini_stream(), httpx.Response(503)])
    install(monkeypatch, lambda r: next(responses))
    (gateway.data_dir()/'model-prices.json').write_text(json.dumps(
        {'models': {'gemini': {'gemini-test': {'input': 1, 'output': 2}}}}))
    for _ in range(2):
        gateway.complete(request(), KEYS)
    with pytest.raises(ModelError):
        gateway.complete(request(), KEYS)
    [group] = gateway.summary()
    assert (group['provider'], group['model'], group['stage']) == ('gemini', 'gemini-test', 'plan')
    assert group['calls'] == 3 and group['failures'] == 1
    assert group['p50_latency_ms'] is not None and group['p95_latency_ms'] >= group['p50_latency_ms']
    assert group['input_tokens'] == 200 and group['cost_usd'] == pytest.approx(2*200/1e6)
    assert group['cost_unknown_calls'] == 1


# Routing and replay --------------------------------------------------------

def test_default_routing_and_override(monkeypatch):
    defaults = gateway.routing()['defaults']
    assert set(defaults) >= {'map', 'discover', 'plan', 'review', 'repair', 'recommend', 'other'}
    assert defaults['plan'] == {'provider': 'gemini', 'model': 'gemini-3.8-flash'}
    assert gateway.routing()['fallbacks'] == {}
    gateway.save_routing({'defaults': {'plan': {'provider': 'anthropic', 'model': 'claude-sonnet-4-5'}}})
    assert (gateway.data_dir()/'model-routing.json').exists()
    seen = capture(monkeypatch, anthropic_stream())
    result = gateway.complete(request(provider=None, model=None), KEYS)
    assert (result.provider, result.model) == ('anthropic', 'claude-sonnet-4-5')
    assert seen[0].url.host == 'api.anthropic.com'
    assert gateway.routing()['defaults']['map']['provider'] == 'gemini'


@pytest.mark.parametrize('bad', [{'defaults': {'nope': {'provider': 'gemini', 'model': 'm'}}},
                                  {'defaults': {'plan': {'provider': 'acme', 'model': 'm'}}},
                                  {'defaults': {'plan': {'provider': 'gemini'}}}, {'extra': 1}, [], 'x'])
def test_invalid_routing_is_rejected(bad):
    with pytest.raises(ValueError):
        gateway.save_routing(bad)
    assert not (gateway.data_dir()/'model-routing.json').exists()


def test_no_silent_fallback_when_none_configured(monkeypatch):
    seen = capture(monkeypatch, httpx.Response(503))
    with pytest.raises(ModelError) as error:
        gateway.complete(request(provider=None, model=None), KEYS)
    assert error.value.category == 'service' and len(seen) == 1 and len(gateway.calls(10)) == 1


def test_explicit_fallback_is_a_separate_logged_call(monkeypatch):
    gateway.save_routing({'fallbacks': {'plan': [{'provider': 'openai', 'model': 'gpt-test'}]}})
    def handler(http_request):
        return httpx.Response(503) if http_request.url.host.startswith('generativelanguage') else openai_stream()
    install(monkeypatch, handler)
    result = gateway.complete(request(provider=None, model=None), KEYS)
    assert result.provider == 'openai' and result.attempts == 2
    rows = gateway.calls(10)
    assert sorted((r['provider'], r['status']) for r in rows) == [('gemini', 'error'), ('openai', 'ok')]


def test_auth_errors_do_not_fall_back(monkeypatch):
    gateway.save_routing({'fallbacks': {'plan': [{'provider': 'openai', 'model': 'gpt-test'}]}})
    seen = capture(monkeypatch, httpx.Response(401))
    with pytest.raises(ModelError):
        gateway.complete(request(provider=None, model=None), KEYS)
    assert len(seen) == 1


def test_replay_resends_archived_request_to_another_model(monkeypatch):
    seen = []
    def handler(http_request):
        seen.append(http_request)
        return gemini_stream() if http_request.url.host.startswith('generativelanguage') else openai_stream()
    install(monkeypatch, handler)
    original = gateway.complete(request(prompt_version='editor-8'), KEYS)
    seen.clear()
    replayed = gateway.replay(original.call_id, 'openai', 'gpt-test', keys=KEYS)
    assert replayed.json == {'ok': True} and replayed.provider == 'openai'
    body = json.loads(seen[0].content)
    assert body['input'][0]['content'][-1]['text'] == 'Pick the best excerpt.'
    row = gateway.get_call(replayed.call_id)
    assert row['replay_of'] == original.call_id and row['prompt_version'] == 'editor-8'
    assert row['collection_id'] == 'c1' and replayed.call_id != original.call_id


def test_replay_needs_images_when_original_had_them(monkeypatch):
    capture(monkeypatch, gemini_stream())
    image = {'mime': 'image/jpeg', 'data': b'pixels'}
    original = gateway.complete(request(images=[image]), KEYS)
    with pytest.raises(ModelError) as error:
        gateway.replay(original.call_id, 'gemini', 'other-model', keys=KEYS)
    assert error.value.category == 'config'
    assert gateway.replay(original.call_id, 'gemini', 'other-model', keys=KEYS, images=[image]).json == {'ok': True}
    with pytest.raises(ModelError):
        gateway.replay('missing', 'gemini', 'm', keys=KEYS)


def test_local_qwen_provider_uses_worker_without_streaming(monkeypatch):
    from clippie import local_editor
    seen = []
    def fake(messages, max_tokens=1200, wait=True):
        seen.append((messages, max_tokens))
        return {'text': '{"ok":true}', 'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8},
                'finish_reason': 'stop'}
    monkeypatch.setattr(local_editor, 'generate', fake)
    result = gateway.complete(request(provider='local_qwen', model=local_editor.MODEL_ID, max_output_tokens=5000), {})
    assert result.json == {'ok': True} and seen[0][1] == 1200 and result.usage['total_tokens'] == 8


def test_a_fallback_without_its_key_is_skipped_and_never_blocks_the_primary(monkeypatch):
    # Real failure: discovery routed to Vertex (ready) with an OpenRouter fallback that had no key; the call
    # was refused before Vertex was ever tried.
    gateway.save_routing({'fallbacks': {'plan': [{'provider': 'openrouter', 'model': 'free-model'},
                                                 {'provider': 'openai', 'model': 'gpt-test'}]}})
    hosts = []
    def handler(http_request):
        hosts.append(http_request.url.host)
        return httpx.Response(503) if http_request.url.host.startswith('generativelanguage') else openai_stream()
    install(monkeypatch, handler)
    keys = {k: v for k, v in KEYS.items() if k != 'openrouter'}
    result = gateway.complete(request(provider=None, model=None), keys)
    assert result.provider == 'openai' and not any('openrouter' in h for h in hosts)
    assert gateway.usable('openrouter', keys) is False and gateway.usable('vertex', {}) is True
