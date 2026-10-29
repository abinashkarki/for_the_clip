"""Shared helpers for model gateway tests: MockTransport streams, no network."""
import json
import time

import httpx

SSE = {'content-type': 'text/event-stream'}


REAL_CLIENT = httpx.Client


def install(monkeypatch, handler):
    """Route every httpx.Client through a MockTransport; safe to call again within a test."""
    monkeypatch.setattr(httpx, 'Client', lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def sse(*events):
    """events: dicts, or (name, dict) pairs."""
    out = ''
    for event in events:
        name, data = event if isinstance(event, tuple) else (None, event)
        out += (f'event: {name}\n' if name else '') + 'data: ' + json.dumps(data) + '\n\n'
    return out


def stream_response(text):
    return httpx.Response(200, headers=SSE, content=text.encode())


def slow(chunks, delay):
    def generate():
        for chunk in chunks:
            time.sleep(delay)
            yield chunk.encode()
    return httpx.Response(200, headers=SSE, content=generate())


def gemini_stream(text='{"ok":true}', finish='STOP', usage=None):
    half = len(text)//2
    first = {'candidates': [{'content': {'parts': [{'text': text[:half]}]}}], 'modelVersion': 'gemini-test-001'}
    last = {'candidates': [{'content': {'parts': [{'thought': True, 'text': 'secret thinking'},
                                                  {'text': text[half:]}]}, 'finishReason': finish}],
            'usageMetadata': usage or {'promptTokenCount': 100, 'candidatesTokenCount': 20,
                                       'thoughtsTokenCount': 30, 'cachedContentTokenCount': 40,
                                       'totalTokenCount': 150},
            'modelVersion': 'gemini-test-001'}
    return stream_response(sse(first, last))


def anthropic_stream(text='{"ok":true}', stop='tool_use'):
    half = len(text)//2
    return stream_response(sse(
        ('message_start', {'type': 'message_start', 'message': {'model': 'claude-test-1',
            'usage': {'input_tokens': 10, 'cache_read_input_tokens': 4, 'cache_creation_input_tokens': 0,
                      'output_tokens': 1}}}),
        ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                 'content_block': {'type': 'tool_use', 'name': 'return_json'}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                 'delta': {'type': 'input_json_delta', 'partial_json': text[:half]}}),
        ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                 'delta': {'type': 'input_json_delta', 'partial_json': text[half:]}}),
        ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop}, 'usage': {'output_tokens': 7}}),
        ('message_stop', {'type': 'message_stop'})))


def openai_stream(text='{"ok":true}', status='completed', incomplete=None):
    half = len(text)//2
    response = {'model': 'gpt-test-2', 'status': status,
                'usage': {'input_tokens': 50, 'output_tokens': 25, 'total_tokens': 75,
                          'input_tokens_details': {'cached_tokens': 10},
                          'output_tokens_details': {'reasoning_tokens': 5}}}
    if incomplete:
        response['incomplete_details'] = {'reason': incomplete}
    return stream_response(sse(
        ('response.created', {'type': 'response.created', 'response': {}}),
        ('response.output_text.delta', {'type': 'response.output_text.delta', 'delta': text[:half]}),
        ('response.output_text.delta', {'type': 'response.output_text.delta', 'delta': text[half:]}),
        ('response.completed' if status == 'completed' else 'response.incomplete',
         {'type': 'response.completed' if status == 'completed' else 'response.incomplete', 'response': response})))
