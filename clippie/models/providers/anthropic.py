"""Anthropic Messages API (stream: true). Structured output uses a forced tool."""
from __future__ import annotations

import base64

from ..core import ModelError
from ._http import Raw, events

URL = 'https://api.anthropic.com/v1/messages'
VERSION = '2023-06-01'
TOOL = 'return_json'
MESSAGES = {400: 'Anthropic rejected this request or model configuration.',
            401: 'Anthropic authentication failed. Check the API key in Settings.',
            403: 'This Anthropic key cannot use the selected model.',
            404: 'This Anthropic model is unavailable to the API key.',
            413: 'The request is too large for Anthropic.',
            429: 'Anthropic quota or rate limit reached.'}
FINISH = {'end_turn': 'stop', 'stop_sequence': 'stop', 'tool_use': 'stop', 'max_tokens': 'length'}
ERRORS = {'authentication_error': 'auth', 'permission_error': 'auth', 'rate_limit_error': 'quota',
          'invalid_request_error': 'config', 'not_found_error': 'config'}


def build_body(req, model):
    content = []
    for image in req.images:
        if image.get('label'):
            content.append({'type': 'text', 'text': image['label']})
        content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': image['mime'],
                                                    'data': base64.b64encode(image['data']).decode()}})
    content.append({'type': 'text', 'text': req.prompt})
    body = {'model': model, 'max_tokens': req.max_output_tokens, 'stream': True,
            'messages': [{'role': 'user', 'content': content}]}
    if req.system:
        body['system'] = req.system
    if req.temperature is not None:
        body['temperature'] = req.temperature
    if req.json_schema is not None:
        body['tools'] = [{'name': TOOL, 'description': 'Return the answer as JSON matching the schema.',
                          'input_schema': req.json_schema}]
        body['tool_choice'] = {'type': 'tool', 'name': TOOL}
    return body


def call(req, model, key):
    headers = {'x-api-key': key, 'anthropic-version': VERSION, 'content-type': 'application/json'}
    raw, text, tool = Raw(req), [], []
    counts = {}
    for name, chunk in events(URL, headers, build_body(req, model), req, 'Anthropic', MESSAGES):
        if name == 'ttft':
            raw.ttft_ms = chunk
            continue
        kind = chunk.get('type') or name
        if kind == 'error':
            category = ERRORS.get((chunk.get('error') or {}).get('type'), 'service')
            raise ModelError(category, 'Anthropic reported an error while streaming.')
        if kind == 'message_start':
            message = chunk.get('message') or {}
            raw.model_version = message.get('model')
            counts.update(message.get('usage') or {})
            raw.usage = usage_of(counts)
        elif kind == 'content_block_delta':
            delta = chunk.get('delta') or {}
            if delta.get('type') == 'text_delta':
                text.append(delta.get('text', ''))
            elif delta.get('type') == 'input_json_delta':
                tool.append(delta.get('partial_json', ''))
        elif kind == 'message_delta':
            counts.update(chunk.get('usage') or {})
            raw.usage = usage_of(counts)
            reason = (chunk.get('delta') or {}).get('stop_reason')
            if reason:
                raw.finish_reason = FINISH.get(reason, reason)
    raw.text = ''.join(tool) if tool else ''.join(text)
    raw.usage = usage_of(counts) if counts else {}
    return raw


def usage_of(counts):
    cached = counts.get('cache_read_input_tokens') or 0
    created = counts.get('cache_creation_input_tokens') or 0
    base = counts.get('input_tokens')
    total_in = None if base is None else base+cached+created
    output = counts.get('output_tokens')
    return {'input_tokens': total_in, 'output_tokens': output, 'thinking_tokens': None,
            'cached_tokens': cached, 'total_tokens': None if total_in is None or output is None else total_in+output}
