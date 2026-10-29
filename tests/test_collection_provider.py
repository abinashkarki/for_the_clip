import json
import time
from types import SimpleNamespace

import httpx
import pytest

from clippie import collection_provider as provider, gemini


@pytest.fixture
def context():
    usage = []
    return SimpleNamespace(task={'id': 'task', 'lease_token': 'lease',
                                  'reservation': {'tokens': provider.RESERVATION_TOKENS, 'calls': 1},
                                  'payload': {'deadline': time.time() + 120}},
                           checkpoint=lambda: None,
                           store=SimpleNamespace(record_usage=lambda *a, **kw: usage.append(kw)),
                           usage=usage)


def sse(text, tokens):
    usage = {'totalTokenCount': tokens} if tokens is not None else {}
    event = {'usageMetadata': usage, 'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}]}
    return 'data: ' + json.dumps(event) + '\n\n'


def transport(monkeypatch, status=200, text='{}', tokens=9):
    posts = []
    def handler(request):
        posts.append((str(request.url), request, request.extensions['timeout']))
        if status != 200:
            return httpx.Response(status, json={'error': {'message': 'private'}})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=sse(text, tokens))
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return posts


def call(ctx):
    return provider.invoke(ctx, 'private-key', 'plan', 'source evidence', {'type': 'object'}, 100)


@pytest.mark.parametrize('status', [503, 429, 401])
def test_one_http_request_for_transient_and_auth(monkeypatch, context, status):
    posts = transport(monkeypatch, status)
    monkeypatch.setattr(gemini.time, 'sleep', lambda _: pytest.fail('hidden retry'))
    with pytest.raises(provider.CollectionProviderError) as failure:
        call(context)
    assert len(posts) == 1
    assert context.usage == [{'actual_calls': 1}]
    assert 'private-key' not in str(failure.value)


def test_invalid_output_retains_known_usage(monkeypatch, context):
    posts = transport(monkeypatch, text='invalid json')
    with pytest.raises(provider.CollectionProviderError, match='provider_invalid_output'):
        call(context)
    assert len(posts) == 1
    assert context.usage == [{'actual_calls': 1, 'actual_tokens': 9}]


def test_success_deadline_and_credentials(monkeypatch, context):
    posts = transport(monkeypatch)
    result = call(context)
    assert result['proposal'] == {} and result['provider_requests'] == 1
    url, request, timeout = posts[0]
    assert timeout['read'] <= provider.REQUEST_SECONDS
    assert 'private-key' not in url
    assert 'private-key' not in request.content.decode()
    assert request.headers['x-goog-api-key'] == 'private-key'


@pytest.mark.parametrize('failure', ['oversize', 'reservation', 'cancelled', 'deadline', 'output'])
def test_rejected_before_http(monkeypatch, context, failure):
    posts = transport(monkeypatch)
    if failure == 'reservation':
        context.task['reservation']['tokens'] = 0
    elif failure == 'cancelled':
        def cancelled():
            raise RuntimeError('cancelled')
        context.checkpoint = cancelled
    elif failure == 'deadline':
        context.task['payload']['deadline'] = time.time() - 1
    with pytest.raises(RuntimeError):
        provider.invoke(context, 'private-key', 'plan',
                        'é' * provider.INPUT_BYTE_CAP if failure == 'oversize' else 'evidence',
                        {'type': 'object'}, 4097 if failure == 'output' else 100)
    assert posts == [] and context.usage == []


def test_unknown_usage_keeps_reservation(monkeypatch, context):
    transport(monkeypatch, text='bad', tokens=None)
    with pytest.raises(provider.CollectionProviderError):
        call(context)
    assert context.usage == [{'actual_calls': 1}]


def test_default_v1_request_cap():
    assert gemini.RequestBudget().max_requests == 4


def picture():
    import base64
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (240, 135), 'blue').save(buffer, 'JPEG')
    return {'id': 'still1', 'at': 1.5, 'source': 's', 'data': base64.b64encode(buffer.getvalue()).decode()}


def test_stills_share_one_bounded_http_attempt_and_usage(monkeypatch, context):
    posts = transport(monkeypatch)
    frame = picture()
    provider.invoke(context, 'private-key', 'map', 'evidence', {'type': 'object'}, 100, pictures=[frame])
    assert len(posts) == 1 and context.usage == [{'actual_calls': 1, 'actual_tokens': 9}]
    parts = json.loads(posts[0][1].content)['contents'][0]['parts']
    assert parts[-1]['inlineData'] == {'mimeType': 'image/jpeg', 'data': frame['data']}


@pytest.mark.parametrize('fault', ['base64', 'metadata', 'count', 'budget'])
def test_bad_or_oversized_visual_input_stops_before_http(monkeypatch, context, fault):
    posts = transport(monkeypatch)
    frame = picture(); frames = [frame]; prompt = 'evidence'
    if fault == 'base64': frame['data'] = 'invalid!'
    if fault == 'metadata': frame['source'] = '../private'
    if fault == 'count': frames *= 3
    if fault == 'budget': prompt = 'x' * provider.INPUT_BYTE_CAP
    with pytest.raises(provider.CollectionProviderError):
        provider.invoke(context, 'private-key', 'map', prompt, {'type': 'object'}, 100, pictures=frames)
    assert not posts and not context.usage
