"""Streaming HTTP shared by cloud providers: idle timeout resets on every chunk."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import math
import queue
import threading
import time

import httpx

from ..core import ModelError

_DONE = object()


def retry_after(headers):
    value = headers.get('Retry-After') if headers is not None else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


SECRET = re.compile(r'(AIza[0-9A-Za-z_\-]{20,}|sk-[0-9A-Za-z_\-]{16,}|nvapi-[0-9A-Za-z_\-]{16,}|Bearer\s+\S+)')


def provider_detail(content):
    """The provider's own error message, short and with anything key-like removed. Never the request."""
    try:
        data = json.loads(content.decode('utf-8', 'replace'))
    except (ValueError, AttributeError):
        return None
    error = data.get('error') if isinstance(data, dict) else None
    message = error.get('message') if isinstance(error, dict) else error if isinstance(error, str) else None
    if not isinstance(message, str) or not message.strip():
        return None
    return SECRET.sub('[redacted]', ' '.join(message.split()))[:400]


def classify_status(status, messages, name):
    """HTTP status to a safe ModelError. Provider bodies are never echoed."""
    category = ('quota' if status == 429 else 'auth' if status in {401, 403}
                else 'config' if status in {400, 402, 404, 413, 422} else 'service')
    return category, messages.get(status, f'{name} is temporarily unavailable.')


def stream_lines(url, headers, body, idle, total, *, timings=None):
    """Yield ('status', code, headers) then ('line', text) / ('body', bytes) items.

    A reader thread feeds a queue so a stalled connection is detected by the
    consumer regardless of transport: no chunk for `idle` seconds, or `total`
    seconds overall, raises a timeout ModelError and abandons the connection.
    """
    items = queue.Queue()
    stop = threading.Event()
    started = time.monotonic()
    timings = timings if timings is not None else {}
    timings['transport_started_at'] = time.time()

    def pump():
        try:
            with httpx.Client(timeout=httpx.Timeout(idle+5, connect=min(10, total)), follow_redirects=False) as client:
                with client.stream('POST', url, headers=headers, json=body) as response:
                    if stop.is_set():
                        return
                    timings['response_headers_ms'] = int((time.monotonic()-started)*1000)
                    if response.status_code != 200:
                        response.read()
                        items.put(('status', response.status_code, response.headers, provider_detail(response.content)))
                        return
                    items.put(('status', response.status_code, response.headers))
                    for line in response.iter_lines():
                        if stop.is_set():
                            return
                        timings.setdefault('first_response_ms', int((time.monotonic()-started)*1000))
                        items.put(('line', line))
        except httpx.TimeoutException:
            items.put(('error', 'timeout'))
        except httpx.HTTPError:
            items.put(('error', 'network'))
        except Exception:
            items.put(('error', 'network'))
        finally:
            items.put(_DONE)

    threading.Thread(target=pump, daemon=True, name='model-stream').start()
    deadline = time.monotonic()+total
    try:
        while True:
            wait = min(idle, deadline-time.monotonic())
            if wait <= 0:
                raise ModelError('timeout', 'The model request exceeded its total time limit.')
            try:
                item = items.get(timeout=wait)
            except queue.Empty:
                limit = 'total time limit' if deadline-time.monotonic() <= 0 else 'idle limit (no data received)'
                raise ModelError('timeout', f'The model request reached its {limit}.') from None
            if item is _DONE:
                return
            if item[0] == 'error':
                raise ModelError(item[1], 'The model request timed out.' if item[1] == 'timeout'
                                 else 'Could not reach the model API.')
            yield item
    finally:
        stop.set()


def loads(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


class SSEParser:
    """Incremental server-sent-events parser fed one line at a time."""

    def __init__(self):
        self.event, self.data = None, []

    def feed(self, line):
        if line == '':
            return self.flush()
        if line.startswith('event:'):
            self.event = line[6:].strip()
        elif line.startswith('data:'):
            self.data.append(line[5:].lstrip(' '))
        return None

    def flush(self):
        if not self.data:
            self.event = None
            return None
        result = (self.event, '\n'.join(self.data))
        self.event, self.data = None, []
        return result


class Raw:
    """What a provider call produced, before gateway validation and logging."""

    def __init__(self, req=None):
        self.text = ''
        self.usage = {}
        self.model_version = None
        self.finish_reason = None
        self.ttft_ms = None
        if req is not None:
            req._raw = self


def events(url, headers, body, req, name, messages):
    """POST and yield ('ttft', ms) once, then (event_name, parsed_json) per SSE event.

    Non-200 statuses raise a classified ModelError. A plain JSON body (no SSE
    framing) is accepted as one event, or several when it is a JSON array.
    """
    started = time.monotonic()
    timings = getattr(req, '_timings', None)
    if timings is None:
        timings = req._timings = {}
    first, plain, lines, parser = True, False, [], SSEParser()
    def decoded(event, data):
        for name_, chunk in _decode(event, data):
            if _meaningful_content(chunk):
                timings.setdefault('first_content_ms', int((time.monotonic()-started)*1000))
            yield name_, chunk
    for item in stream_lines(url, headers, body, req.idle_timeout, req.total_timeout, timings=timings):
        if item[0] == 'status':
            if item[1] != 200:
                category, message = classify_status(item[1], messages, name)
                error = ModelError(category, message, status_code=item[1], retry_after=retry_after(item[2]))
                error.detail = item[3] if len(item) > 3 else None
                raise error
            plain = 'text/event-stream' not in item[2].get('content-type', '')
            continue
        if first:
            first = False
            yield 'ttft', int((time.monotonic()-started)*1000)
        if plain:
            lines.append(item[1])
            continue
        done = parser.feed(item[1])
        if done:
            yield from decoded(*done)
    done = parser.flush()
    if done:
        yield from decoded(*done)
    if plain and lines:
        whole = loads('\n'.join(lines))
        for chunk in (whole if isinstance(whole, list) else [whole]):
            if isinstance(chunk, dict):
                if _meaningful_content(chunk):
                    timings.setdefault('first_content_ms', int((time.monotonic()-started)*1000))
                yield None, chunk


def _meaningful_content(chunk):
    """Output text/tool JSON, excluding metadata, keepalives and hidden reasoning.

    This is decoded client content availability, not provider inference/queue
    time. first_response_ms measures a complete received line, never raw bytes.
    """
    if chunk.get('type') == 'response.output_text.delta':
        return bool(chunk.get('delta'))
    delta = chunk.get('delta')
    if isinstance(delta, dict) and delta.get('type') in {'text_delta', 'input_json_delta'}:
        return bool(delta.get('text') or delta.get('partial_json'))
    for choice in chunk.get('choices') or []:
        if (choice.get('delta') or {}).get('content'):
            return True
    for candidate in chunk.get('candidates') or []:
        if any(part.get('text') and not part.get('thought')
               for part in (candidate.get('content') or {}).get('parts') or []):
            return True
    return False


def _decode(event, data):
    payload = loads(data)
    if isinstance(payload, dict):
        yield event, payload
