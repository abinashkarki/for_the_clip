"""OpenAI-style chat-completions streaming for NVIDIA NIM, OpenRouter, Groq and local servers.

One adapter, many endpoints. Structured output uses `response_format: json_schema` (non-strict, so optional
fields keep their meaning) and the schema is repeated in the system message for servers that ignore the
format field; the gateway validates the result locally either way.
"""
from __future__ import annotations

import base64
import json
import os

from ..core import ModelError
from ._http import Raw, events, classify_status, provider_detail

ENDPOINTS = {
    'nvidia': {'url': 'https://integrate.api.nvidia.com/v1/chat/completions', 'name': 'NVIDIA NIM', 'key': True},
    'openrouter': {'url': 'https://openrouter.ai/api/v1/chat/completions', 'name': 'OpenRouter', 'key': True,
                   'headers': {'HTTP-Referer': 'http://127.0.0.1:8765', 'X-Title': 'Clippie'}},
    'groq': {'url': 'https://api.groq.com/openai/v1/chat/completions', 'name': 'Groq', 'key': True},
    'ollama': {'url': 'http://127.0.0.1:11434/v1/chat/completions', 'name': 'Local server', 'key': False,
               'env': 'CLIPPIE_LOCAL_LLM_URL'},
}


def messages(name):
    return {400: f'{name} rejected this request or model configuration.',
            401: f'{name} authentication failed. Check the API key in Settings.',
            402: f'{name} needs credits for this model.',
            403: f'This {name} key cannot use the selected model.',
            404: f'This {name} model is unavailable.',
            413: f'The request is too large for {name}.',
            429: f'{name} quota or rate limit reached.'}


def url_for(provider):
    endpoint = ENDPOINTS[provider]
    override = endpoint.get('env') and os.environ.get(endpoint['env'], '').strip()
    if override:
        base = override.rstrip('/')
        return base if base.endswith('/chat/completions') else base + '/chat/completions'
    return endpoint['url']


def build_body(req, model):
    system = req.system or ''
    if req.json_schema is not None:
        system = (system + '\n\n' if system else '') + (
            'Reply with one JSON object only, no prose or code fences, matching this JSON Schema:\n'
            + json.dumps(req.json_schema, separators=(',', ':')))
    content = []
    for image in req.images:
        if image.get('label'):
            content.append({'type': 'text', 'text': image['label']})
        data = base64.b64encode(image['data']).decode()
        content.append({'type': 'image_url', 'image_url': {'url': f"data:{image['mime']};base64,{data}"}})
    content.append({'type': 'text', 'text': req.prompt})
    user = {'role': 'user', 'content': content if req.images else req.prompt}
    body = {'model': model, 'stream': True, 'stream_options': {'include_usage': True},
            'max_tokens': req.max_output_tokens, 'messages': ([{'role': 'system', 'content': system}] if system else []) + [user]}
    if req.temperature is not None:
        body['temperature'] = req.temperature
    if req.json_schema is not None:
        body['response_format'] = {'type': 'json_schema',
                                   'json_schema': {'name': 'response', 'strict': False, 'schema': req.json_schema}}
    for key, value in (req.options or {}).items():   # routing extras, e.g. chat_template_kwargs or reasoning_effort
        body[key] = value
    return body


def usage_of(usage):
    details = usage.get('completion_tokens_details') or {}
    reasoning = details.get('reasoning_tokens') or 0
    output = usage.get('completion_tokens')
    return {'input_tokens': usage.get('prompt_tokens'),
            'output_tokens': None if output is None else max(0, output - reasoning),
            'thinking_tokens': reasoning, 'total_tokens': usage.get('total_tokens'),
            'cached_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0}


FINISH = {'stop': 'stop', 'length': 'length', 'content_filter': 'blocked', 'eos': 'stop', 'end_turn': 'stop'}


def strip_fences(text):
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else ''
        if text.rstrip().endswith('```'):
            text = text.rstrip()[:-3]
    return text.strip()


def call(req, model, key, provider):
    endpoint = ENDPOINTS[provider]
    name = endpoint['name']
    headers = {'Content-Type': 'application/json', **endpoint.get('headers', {})}
    if key:
        headers['Authorization'] = f'Bearer {key}'
    raw, text = Raw(req), []
    reasoning = 0
    try:
        for event, chunk in events(url_for(provider), headers, build_body(req, model), req, name, messages(name)):
            reasoning += _consume(event, chunk, raw, text, name)
    except ModelError as error:
        error.partial = {'ttft_ms': raw.ttft_ms, 'reasoning_chars': reasoning, 'text_chars': sum(map(len, text))}
        raise
    raw.text = strip_fences(''.join(text)) if req.json_schema is not None else ''.join(text)
    return raw


def _consume(event, chunk, raw, text, name):
    """Apply one stream event; returns the number of reasoning characters it carried."""
    if event == 'ttft':
        raw.ttft_ms = chunk
        return 0
    if not isinstance(chunk, dict):
        return 0
    if chunk.get('error'):
        code = chunk['error'].get('code') if isinstance(chunk['error'], dict) else None
        detail = provider_detail(json.dumps({'error': chunk['error']}).encode())
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 429 if code == 'rate_limit_exceeded' else None
        category = classify_status(status, messages(name), name)[0] if status is not None else 'service'
        if detail and 'multimodal processing is not enabled' in detail.lower():
            category = 'config'
        error = ModelError(category,
                           f'{name} reported an error while streaming.')
        error.detail = detail
        error.status_code = status
        raise error
    raw.model_version = chunk.get('model') or raw.model_version
    if chunk.get('usage'):
        raw.usage = usage_of(chunk['usage'])
    thought = 0
    for choice in chunk.get('choices') or []:
        delta = choice.get('delta') or {}
        if delta.get('content'):
            text.append(delta['content'])
        for key in ('reasoning_content', 'reasoning'):
            if isinstance(delta.get(key), str):
                thought += len(delta[key])
        if choice.get('finish_reason'):
            raw.finish_reason = FINISH.get(choice['finish_reason'], choice['finish_reason'])
    return thought


def provider_module(provider):
    """A module-like object exposing call(req, model, key) for the gateway's PROVIDERS table."""
    class Endpoint:
        NAME = ENDPOINTS[provider]['name']
        NEEDS_KEY = ENDPOINTS[provider]['key']

        @staticmethod
        def call(req, model, key):
            return call(req, model, key, provider)
    Endpoint.__name__ = f'chat_compat_{provider}'
    return Endpoint
