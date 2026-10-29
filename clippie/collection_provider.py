"""Bounded editorial transport with durable admission of each physical request."""
from __future__ import annotations

import json
import math
import time
import base64
import io
import re

from . import gemini
from .models import gateway, ModelError, ModelRequest

INPUT_BYTE_CAP = 24_576
OUTPUT_TOKEN_CAP = 4_096
RESERVATION_TOKENS = INPUT_BYTE_CAP + OUTPUT_TOKEN_CAP
RESERVATION_CALLS = 1
# Batched stages (volume discovery, batched review and repair) carry up to 30 candidates in one request.
VOLUME_INPUT_BYTE_CAP = 131_072
VOLUME_OUTPUT_TOKEN_CAP = 16_384
VOLUME_RESERVATION_TOKENS = VOLUME_INPUT_BYTE_CAP + VOLUME_OUTPUT_TOKEN_CAP
REQUEST_SECONDS = 90
IDLE_SECONDS = 45
# Thinking models can be silent for a long time before the first token of a large JSON answer.
VOLUME_REQUEST_SECONDS = 240
VOLUME_IDLE_SECONDS = 150
# Stage names are a closed editorial protocol, never model/endpoint selectors.
MAPPED = frozenset({'auth', 'quota', 'config', 'service', 'network', 'timeout', 'output_limit', 'retry_budget'})
STAGES = frozenset({'map', 'discover', 'plan', 'review', 'repair'})


class CollectionProviderError(RuntimeError):
    def __init__(self, category):
        super().__init__(category)
        self.category = category


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_pictures(pictures):
    """Only bounded still JPEGs enter the editorial transport, never local paths."""
    from PIL import Image
    if not isinstance(pictures, (list, tuple)) or len(pictures) > 2:
        raise CollectionProviderError('provider_config')
    seen = set()
    for picture in pictures:
        if (not isinstance(picture, dict) or set(picture) != {'id', 'at', 'source', 'data'}
                or not isinstance(picture['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', picture['id'])
                or picture['id'] in seen or not isinstance(picture['source'], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', picture['source'])
                or isinstance(picture['at'], bool) or not isinstance(picture['at'], (int, float))
                or not math.isfinite(picture['at']) or picture['at'] < 0
                or not isinstance(picture['data'], str) or len(picture['data']) > 16_000):
            raise CollectionProviderError('provider_config')
        seen.add(picture['id'])
        try:
            raw = base64.b64decode(picture['data'], validate=True)
            with Image.open(io.BytesIO(raw)) as frame:
                if frame.format != 'JPEG' or not 0 < frame.width <= 320 or not 0 < frame.height <= 180:
                    raise ValueError('Invalid sampled still')
                frame.verify()
        except (ValueError, OSError):
            raise CollectionProviderError('provider_config') from None


def input_size(prompt, schema, pictures=()):
    # Includes text headers used by Gemini for each attached JPEG, plus envelope slack.
    return len((gemini.SYSTEM + prompt + json.dumps(schema, ensure_ascii=True, allow_nan=False)
                + json.dumps(pictures, ensure_ascii=True, allow_nan=False)).encode('utf-8')) + 512


def invoke(ctx, api_key, stage, prompt, schema, max_tokens, *, pictures=(), volume=False):
    """Admit each configured transport attempt; reconcile even on output failure.

    Deadline is an absolute Unix timestamp in task.payload.deadline. Reservations
    cover the complete configured envelope, not an estimate of a particular prompt.
    """
    ctx.checkpoint()
    if stage not in STAGES or not isinstance(api_key, str):
        raise CollectionProviderError('provider_config')
    input_cap = VOLUME_INPUT_BYTE_CAP if volume else INPUT_BYTE_CAP
    output_cap = VOLUME_OUTPUT_TOKEN_CAP if volume else OUTPUT_TOKEN_CAP
    needed = input_cap + output_cap
    if stage == 'map':
        # A map call that needs the larger envelope reserves what it actually sends, not the whole envelope.
        output_cap = max(output_cap, max_tokens) if volume else output_cap
    if (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
            or not 0 < max_tokens <= output_cap):
        raise CollectionProviderError('provider_config')
    if not isinstance(prompt, str) or not isinstance(schema, dict):
        raise CollectionProviderError('provider_config')
    validate_pictures(pictures)
    try:
        size = input_size(prompt, schema, pictures)
    except (ValueError, TypeError, UnicodeError):
        raise CollectionProviderError('provider_config') from None
    if size > input_cap:
        raise CollectionProviderError('provider_input_limit')
    if stage == 'map' and volume:
        needed = min(needed, size + max_tokens)
    reservation = ctx.task.get('reservation', {})
    if (not _count(reservation.get('tokens')) or reservation['tokens'] < needed
            or not _count(reservation.get('calls')) or reservation['calls'] < RESERVATION_CALLS):
        raise CollectionProviderError('provider_reservation')
    deadline = ctx.task.get('payload', {}).get('deadline')
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= time.time()):
        raise CollectionProviderError('provider_deadline')
    keys = _keys(api_key)
    request = ModelRequest(
        stage=stage, prompt=prompt, system=gemini.SYSTEM, json_schema=schema, max_output_tokens=max_tokens,
        images=[{'mime': 'image/jpeg', 'data': base64.b64decode(p['data']),
                 'label': f"Picture {p['id']} at {p['at']:.2f}s in {p['source']}."} for p in pictures],
        total_timeout=min(VOLUME_REQUEST_SECONDS if volume else REQUEST_SECONDS, deadline - time.time()),
        idle_timeout=VOLUME_IDLE_SECONDS if volume else IDLE_SECONDS,
        metadata={'collection_id': ctx.task.get('collection_id'), 'task_id': ctx.task.get('id')})
    request.idle_timeout = min(request.idle_timeout, request.total_timeout)
    # Only the primary route must be callable. A fallback without a key is skipped by the gateway; requiring it
    # here stopped every discovery while Vertex was ready (an OpenRouter fallback had no key).
    chain = gateway.targets(request)
    primary = chain[0][0] if chain else None
    if not gateway.usable(primary, keys):
        raise CollectionProviderError('provider_config')
    ledger = hasattr(ctx.store, 'admit_invocation')
    if ledger:
        def admit(identity):
            ctx.checkpoint()
            try:
                ctx.store.admit_invocation(ctx.task['id'], ctx.task['lease_token'], identity, needed)
            except ValueError as error:
                if str(error) == 'invocation_budget_exhausted':
                    raise CollectionProviderError('provider_retry_budget') from None
                raise
        def reconcile(identity, usage):
            total = usage.get('total_tokens')
            ctx.store.reconcile_invocation(ctx.task['id'], ctx.task['lease_token'], identity,
                                           total if _count(total) else None)
        request.before_attempt = admit
        request.after_attempt = reconcile
        request.checkpoint = ctx.checkpoint
    known = {}
    attempts = 0
    ctx.checkpoint()
    if deadline <= time.time():
        raise CollectionProviderError('provider_deadline')
    try:
        attempts = 1
        result = gateway.complete(request, keys)
        attempts = result.attempts
        total = result.usage.get('total_tokens')
        if _count(total):
            known['actual_tokens'] = total
        if not isinstance(result.json, dict):
            raise ValueError('Not an object')
        return {'proposal': result.json, 'usage': gemini.usage_metrics(result.usage), 'model': result.model,
                'provider': result.provider, 'provider_requests': attempts,
                'revision': result.model_version or result.model, 'finish_reason': 'STOP', 'call_id': result.call_id}
    except ModelError as error:
        attempts = error.attempts
        total = error.usage.get('total_tokens')
        if _count(total):
            known['actual_tokens'] = total
        category = error.category if error.category in MAPPED else 'invalid_output'
        raise CollectionProviderError('provider_' + category) from None
    except CollectionProviderError:
        raise
    except (RuntimeError, ValueError, KeyError, TypeError):
        raise CollectionProviderError('provider_invalid_output') from None
    finally:
        if attempts and not ledger:
            ctx.store.record_usage(ctx.task['id'], ctx.task['lease_token'],
                                   actual_calls=attempts, **known)
        ctx.checkpoint()


def _keys(api_key):
    """The caller's Gemini key plus any other configured provider keys."""
    keys = {}
    try:
        from . import app
        keys = {name: value for name, value in app.KEYS.items() if value}
    except Exception:
        pass
    return {**keys, 'gemini': api_key}
