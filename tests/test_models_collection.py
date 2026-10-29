"""collection_provider.invoke runs through the gateway and keeps its budget behaviour."""
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from clippie import app, collection_provider as provider
from clippie.models import gateway
from tests.support_models import anthropic_stream, gemini_stream, install, slow, sse


@pytest.fixture
def context():
    usage = []
    return SimpleNamespace(task={'id': 'task', 'collection_id': 'col', 'lease_token': 'lease',
                                  'reservation': {'tokens': provider.RESERVATION_TOKENS, 'calls': 1},
                                  'payload': {'deadline': time.time() + 120}},
                           checkpoint=lambda: None,
                           store=SimpleNamespace(record_usage=lambda *a, **kw: usage.append(kw)),
                           usage=usage)


def call(ctx, key='gemini-key', stage='plan'):
    return provider.invoke(ctx, key, stage, 'evidence', {'type': 'object'}, 100)


def test_success_is_logged_with_task_metadata_and_provider_neutral_result(monkeypatch, context):
    install(monkeypatch, lambda request: gemini_stream('{"a":1}'))
    result = call(context)
    assert result['proposal'] == {'a': 1} and result['provider_requests'] == 1
    assert result['usage'] == {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 150}
    assert context.usage == [{'actual_calls': 1, 'actual_tokens': 150}]
    row = gateway.get_call(result['call_id'])
    assert (row['stage'], row['collection_id'], row['task_id']) == ('plan', 'col', 'task')
    assert row['request']['system'] == provider.gemini.SYSTEM


def test_gateway_errors_keep_existing_collection_categories(monkeypatch, context):
    for status, category in [(401, 'auth'), (429, 'quota'), (503, 'service'), (400, 'config')]:
        gateway._COOLDOWNS.clear()  # Independent transport-classification episodes.
        install(monkeypatch, lambda request, status=status: httpx.Response(status))
        context.usage.clear()
        with pytest.raises(provider.CollectionProviderError) as failure:
            call(context)
        assert failure.value.category == 'provider_' + category
        assert context.usage == [{'actual_calls': 1}]


def test_schema_violation_and_truncation_map_and_keep_usage(monkeypatch, context):
    install(monkeypatch, lambda request: gemini_stream('["not an object"]'))
    with pytest.raises(provider.CollectionProviderError, match='provider_invalid_output'):
        call(context)
    assert context.usage == [{'actual_calls': 1, 'actual_tokens': 150}]
    context.usage.clear()
    install(monkeypatch, lambda request: gemini_stream('{"a"', finish='MAX_TOKENS'))
    with pytest.raises(provider.CollectionProviderError, match='provider_output_limit'):
        call(context)
    assert context.usage == [{'actual_calls': 1, 'actual_tokens': 150}]


def test_stalled_stream_times_out_inside_the_task_deadline(monkeypatch, context):
    first = sse({'candidates': [{'content': {'parts': [{'text': '{'}]}}]})
    install(monkeypatch, lambda request: slow([first, 'data: {}\n\n'], 1.0))
    monkeypatch.setattr(provider, 'IDLE_SECONDS', 0.2)
    started = time.monotonic()
    with pytest.raises(provider.CollectionProviderError, match='provider_timeout'):
        call(context)
    assert time.monotonic()-started < 0.9 and context.usage == [{'actual_calls': 1}]


def test_one_request_per_invoke_even_on_transient_failure(monkeypatch, context):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(503)
    install(monkeypatch, handler)
    with pytest.raises(provider.CollectionProviderError):
        call(context)
    assert len(seen) == 1


def test_routing_override_sends_stage_to_another_provider(monkeypatch, context):
    gateway.save_routing({'defaults': {'review': {'provider': 'anthropic', 'model': 'claude-sonnet-4-5'}}})
    monkeypatch.setattr(app, 'KEYS', {**app.KEYS, 'anthropic': 'anthropic-key'})
    seen = []
    def handler(request):
        seen.append(request)
        return anthropic_stream('{"verdict":"keep"}')
    install(monkeypatch, handler)
    result = call(context, stage='review')
    assert result['proposal'] == {'verdict': 'keep'} and result['provider'] == 'anthropic'
    assert seen[0].headers['x-api-key'] == 'anthropic-key'
    assert context.usage == [{'actual_calls': 1, 'actual_tokens': 21}]


def test_routed_provider_without_a_key_is_config_and_sends_nothing(monkeypatch, context):
    gateway.save_routing({'defaults': {'review': {'provider': 'openai', 'model': 'gpt-5-mini'}}})
    monkeypatch.setattr(app, 'KEYS', {**app.KEYS, 'openai': ''})
    seen = []
    install(monkeypatch, lambda request: seen.append(request))
    with pytest.raises(provider.CollectionProviderError, match='provider_config'):
        call(context, stage='review')
    assert seen == [] and context.usage == []


def test_a_fallback_without_a_key_does_not_block_a_ready_primary(monkeypatch, context):
    gateway.save_routing({'defaults': {'review': {'provider': 'anthropic', 'model': 'claude-sonnet-4-5'}},
                          'fallbacks': {'review': [{'provider': 'openrouter', 'model': 'google/gemma-4-31b-it:free'}]}})
    monkeypatch.setattr(app, 'KEYS', {**app.KEYS, 'anthropic': 'anthropic-key', 'openrouter': ''})
    install(monkeypatch, lambda request: anthropic_stream('{"verdict":"keep"}'))
    assert call(context, stage='review')['provider'] == 'anthropic'


def test_empty_gemini_key_is_config(monkeypatch, context):
    with pytest.raises(provider.CollectionProviderError, match='provider_config'):
        call(context, key='')
