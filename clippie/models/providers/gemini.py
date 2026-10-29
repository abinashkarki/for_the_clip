"""Gemini streamGenerateContent (SSE) transport."""
from __future__ import annotations

import base64
import json

from ..core import ModelError
from ._http import Raw, events, provider_detail

API_ROOT = 'https://generativelanguage.googleapis.com/v1beta'
MESSAGES = {400: 'Gemini rejected this request or model configuration.',
            401: 'Gemini authentication failed. Check the API key in Settings.',
            402: 'The Gemini project needs available API credits. Check billing in AI Studio.',
            403: 'This Gemini key or project cannot use the selected Flash model.',
            404: 'This Flash model is unavailable to the API project.',
            429: 'Gemini quota or rate limit reached. Check the project quota in AI Studio.'}
FINISH = {'STOP': 'stop', 'MAX_TOKENS': 'length'}
# Gemini rejects or chokes on some constraint keywords in nested schemas ("invalid argument" on Flash-Lite,
# "too many states" elsewhere). It gets the shape only; the gateway still validates the full schema locally.
RELAXED = {'maxLength', 'minLength', 'maxItems', 'minItems', 'minimum', 'maximum', 'pattern', 'additionalProperties'}


def provider_schema(schema):
    if isinstance(schema, list):
        return [provider_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in RELAXED:
            continue
        if key == 'properties' and isinstance(value, dict):
            result[key] = {name: provider_schema(sub) for name, sub in value.items()}
        elif key in ('enum', 'required'):
            result[key] = value
        else:
            result[key] = provider_schema(value)
    return result


def build_body(req):
    parts = [{'text': req.prompt}]
    for image in req.images:
        if image.get('label'):
            parts.append({'text': image['label']})
        parts.append({'inlineData': {'mimeType': image['mime'], 'data': base64.b64encode(image['data']).decode()}})
    config = {'maxOutputTokens': req.max_output_tokens, 'thinkingConfig': {'thinkingLevel': 'low'}}
    if req.json_schema is not None:
        config.update(responseMimeType='application/json', responseJsonSchema=provider_schema(req.json_schema))
    if req.temperature is not None:
        config['temperature'] = req.temperature
    body = {'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': config}
    if req.system:
        body['systemInstruction'] = {'parts': [{'text': req.system}]}
    return body


def usage_of(meta):
    return {'input_tokens': meta.get('promptTokenCount'), 'output_tokens': meta.get('candidatesTokenCount'),
            'thinking_tokens': meta.get('thoughtsTokenCount'), 'cached_tokens': meta.get('cachedContentTokenCount'),
            'total_tokens': meta.get('totalTokenCount')}


def call(req, model, key):
    url = f'{API_ROOT}/models/{model}:streamGenerateContent?alt=sse'
    headers = {'x-goog-api-key': key, 'Content-Type': 'application/json'}
    return stream(req, url, headers, 'Gemini', MESSAGES)


def stream(req, url, headers, name, messages):
    """Shared Gemini response format for Developer API and Google Cloud transports."""
    raw, text = Raw(req), []
    for event, chunk in events(url, headers, build_body(req), req, name, messages):
        if event == 'ttft':
            raw.ttft_ms = chunk
            continue
        if 'error' in chunk:
            error = ModelError('quota' if (chunk['error'] or {}).get('code') == 429 else 'service',
                               'Gemini reported an error while streaming.')
            error.detail = provider_detail(json.dumps(chunk).encode())
            raise error
        raw.model_version = chunk.get('modelVersion', raw.model_version)
        if chunk.get('usageMetadata'):
            raw.usage = usage_of(chunk['usageMetadata'])
        for candidate in (chunk.get('candidates') or [])[:1]:
            for part in (candidate.get('content') or {}).get('parts') or []:
                if not part.get('thought'):
                    text.append(part.get('text', ''))
            finish = candidate.get('finishReason')
            if finish:
                raw.finish_reason = FINISH.get(finish, finish.lower())
    raw.text = ''.join(text)
    return raw
