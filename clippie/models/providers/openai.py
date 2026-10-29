"""OpenAI Responses API streaming with strict json_schema output."""
from __future__ import annotations

import base64

from ..core import ModelError
from ._http import Raw, events

URL = 'https://api.openai.com/v1/responses'
MESSAGES = {400: 'OpenAI rejected this request or model configuration.',
            401: 'OpenAI authentication failed. Check the API key in Settings.',
            403: 'This OpenAI key or project cannot use the selected model.',
            404: 'This OpenAI model is unavailable to the API project.',
            413: 'The request is too large for OpenAI.',
            429: 'OpenAI quota or rate limit reached.'}
CODES = {'rate_limit_exceeded': 'quota', 'insufficient_quota': 'quota', 'invalid_api_key': 'auth'}


def strict_schema(schema):
    """Strict mode needs additionalProperties:false and every property required."""
    if isinstance(schema, list):
        return [strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: strict_schema(value) for key, value in schema.items()}
    if result.get('type') == 'object' and isinstance(result.get('properties'), dict):
        result['additionalProperties'] = False
        result['required'] = list(result['properties'])
    return result


def build_body(req, model):
    content = []
    for image in req.images:
        if image.get('label'):
            content.append({'type': 'input_text', 'text': image['label']})
        data = base64.b64encode(image['data']).decode()
        content.append({'type': 'input_image', 'image_url': f"data:{image['mime']};base64,{data}"})
    content.append({'type': 'input_text', 'text': req.prompt})
    body = {'model': model, 'stream': True, 'store': False, 'max_output_tokens': req.max_output_tokens,
            'input': [{'role': 'user', 'content': content}]}
    if req.system:
        body['instructions'] = req.system
    if req.temperature is not None:
        body['temperature'] = req.temperature
    if req.json_schema is not None:
        body['text'] = {'format': {'type': 'json_schema', 'name': 'response', 'strict': True,
                                   'schema': strict_schema(req.json_schema)}}
    return body


def usage_of(usage):
    reasoning = (usage.get('output_tokens_details') or {}).get('reasoning_tokens') or 0
    output = usage.get('output_tokens')
    return {'input_tokens': usage.get('input_tokens'),
            'output_tokens': None if output is None else max(0, output-reasoning),
            'thinking_tokens': reasoning, 'total_tokens': usage.get('total_tokens'),
            'cached_tokens': (usage.get('input_tokens_details') or {}).get('cached_tokens') or 0}


def finish_of(response):
    status = response.get('status')
    if status == 'completed':
        return 'stop'
    reason = (response.get('incomplete_details') or {}).get('reason')
    return 'length' if reason == 'max_output_tokens' else reason or status


def call(req, model, key):
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    raw, text = Raw(req), []
    for name, chunk in events(URL, headers, build_body(req, model), req, 'OpenAI', MESSAGES):
        if name == 'ttft':
            raw.ttft_ms = chunk
            continue
        kind = chunk.get('type') or name
        if kind == 'response.output_text.delta':
            text.append(chunk.get('delta', ''))
        elif kind in {'response.completed', 'response.incomplete', 'response.failed'}:
            response = chunk.get('response') or {}
            raw.model_version = response.get('model')
            if response.get('usage'):
                raw.usage = usage_of(response['usage'])
            if kind == 'response.failed':
                code = (response.get('error') or {}).get('code')
                raise ModelError(CODES.get(code, 'service'), 'OpenAI reported a failed response.', usage=raw.usage)
            raw.finish_reason = finish_of(response)
        elif kind in {'error', 'response.error'}:
            category = CODES.get(chunk.get('code') or (chunk.get('error') or {}).get('code'), 'service')
            raise ModelError(category, 'OpenAI reported an error while streaming.')
    raw.text = ''.join(text)
    return raw
