"""Provider-neutral model gateway: routing, validation, cost and call log.

No silent fallback: a failure is raised unless routing.json lists an explicit
fallback for the stage, and every fallback attempt is its own logged call.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from . import validate
from .core import STAGES, ModelError, ModelRequest, ModelResult
from .providers import anthropic, gemini, local_qwen, openai, vertex
from .providers.chat_compat import ENDPOINTS as CHAT_ENDPOINTS, provider_module
from .store import CallLog, image_record

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROVIDERS = {'vertex': vertex, 'gemini': gemini, 'anthropic': anthropic, 'openai': openai, 'local_qwen': local_qwen,
             **{name: provider_module(name) for name in CHAT_ENDPOINTS}}
NAMES = {'vertex': 'Google Cloud (ADC)', 'gemini': 'Gemini', 'anthropic': 'Anthropic', 'openai': 'OpenAI', 'local_qwen': 'The local editor',
         **{name: endpoint['name'] for name, endpoint in CHAT_ENDPOINTS.items()}}
KEYLESS = {'local_qwen', 'vertex'} | {name for name, endpoint in CHAT_ENDPOINTS.items() if not endpoint['key']}
FALLBACK_CATEGORIES = frozenset({'quota', 'service', 'network', 'timeout'})
_DATA = {'root': None}
_LOGS = {}
_LOCK = threading.Lock()
_COOLDOWNS = {}
_SERVICE_CIRCUITS = {}
_ROUTE_PROBES = {}
SERVICE_FAILURE_WINDOW = 120.0
SERVICE_FAILURE_THRESHOLD = 2
SERVICE_COOLDOWN = 30.0


def configure(root):
    """Pin the data directory (tests, embedding). None restores the default."""
    _DATA['root'] = Path(root) if root else None


def data_dir():
    if _DATA['root']:
        return _DATA['root']
    app = sys.modules.get('clippie.app')
    store = getattr(app, 'STORE', None)
    if store is not None and getattr(store, 'root', None):
        return Path(store.root)
    return Path(os.getenv('CLIPPIE_DATA_DIR', ROOT/'data'))


def log():
    root = data_dir()
    with _LOCK:
        return _LOGS.setdefault(str(root), CallLog(root))


# Routing -------------------------------------------------------------------

def _read(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def routing():
    base = _read(HERE/'routing.json')
    override = _read(data_dir()/'model-routing.json')
    return {'defaults': {**base.get('defaults', {}), **override.get('defaults', {})},
            'fallbacks': {**base.get('fallbacks', {}), **override.get('fallbacks', {})}}


def validate_routing(value):
    """Return the normalised override or raise ValueError with a safe message."""
    if not isinstance(value, dict) or set(value) - {'defaults', 'fallbacks'}:
        raise ValueError('Routing must contain only defaults and fallbacks.')
    result = {'defaults': {}, 'fallbacks': {}}
    for stage, route in (value.get('defaults') or {}).items():
        result['defaults'][_stage(stage)] = _route(route)
    for stage, routes in (value.get('fallbacks') or {}).items():
        if not isinstance(routes, list) or len(routes) > 3:
            raise ValueError('Fallbacks are a list of at most three routes.')
        result['fallbacks'][_stage(stage)] = [_route(route) for route in routes]
    return result


def _stage(stage):
    if stage not in STAGES:
        raise ValueError(f'Unknown stage {stage!r}.')
    return stage


PROTECTED_OPTIONS = {'model', 'messages', 'stream', 'stream_options', 'input', 'contents'}


def _route(route):
    if (not isinstance(route, dict) or not {'provider', 'model'} <= set(route) <= {'provider', 'model', 'options'}
            or route['provider'] not in PROVIDERS or not isinstance(route['model'], str) or not 0 < len(route['model']) <= 200):
        raise ValueError('Each route needs a known provider and a model id.')
    result = {'provider': route['provider'], 'model': route['model']}
    options = route.get('options')
    if options:
        if (not isinstance(options, dict) or set(options) & PROTECTED_OPTIONS
                or len(json.dumps(options)) > 2000):
            raise ValueError('Route options must be a small object of extra request fields.')
        result['options'] = options
    return result


def route_options(stage, provider, model):
    """Extra request fields configured for this exact provider/model on this stage (or its fallbacks)."""
    table = routing()
    chain = [table['defaults'].get(stage) or table['defaults'].get('other') or {}] + list(table['fallbacks'].get(stage, []))
    for route in chain:
        if route.get('provider') == provider and route.get('model') == model:
            return dict(route.get('options') or {})
    return {}


def save_routing(value):
    override = validate_routing(value)
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root/f'.model-routing.{os.getpid()}.tmp'
    temporary.write_text(json.dumps(override, indent=2))
    temporary.replace(root/'model-routing.json')
    return routing()


def usable(provider, keys):
    """True when a route's provider has what it needs to be called (a key, unless it is keyless)."""
    return provider in KEYLESS or bool((keys or {}).get(provider))


def targets(req):
    table = routing()
    route = table['defaults'].get(req.stage) or table['defaults'].get('other') or {}
    if req.provider or req.model:
        provider = req.provider or route.get('provider')
        model = req.model or (route.get('model') if route.get('provider') == provider else None)
        return [(provider, model)]
    chain = [route] + list(table['fallbacks'].get(req.stage, []))
    return [(item.get('provider'), item.get('model')) for item in chain]


CLOUD_STAGES = ('map', 'discover', 'plan', 'review', 'repair')


def missing_keys(keys, stages=CLOUD_STAGES):
    """Display names of routed providers that still need a key for these stages (primary routes only)."""
    table = routing()
    missing = []
    for stage in stages:
        route = table['defaults'].get(stage) or table['defaults'].get('other') or {}
        provider = route.get('provider')
        if provider == 'vertex' and not vertex.configured() and NAMES[provider] not in missing:
            missing.append(NAMES[provider])
        if provider in PROVIDERS and provider not in KEYLESS and not keys.get(provider) and NAMES[provider] not in missing:
            missing.append(NAMES[provider])
    return missing


def keys_message(keys):
    missing = missing_keys(keys)
    if not missing:
        return None
    if 'Google Cloud (ADC)' in missing:
        return 'Sign in with gcloud auth application-default login for Google Cloud, or change model routing, before generating clips.'
    return f"Add a {' and '.join(missing)} API key in Settings, or change model routing, before generating clips."


# Prices --------------------------------------------------------------------

def prices():
    base = _read(HERE/'prices.json')
    models = {provider: dict(items) for provider, items in base.get('models', {}).items()}
    for provider, items in _read(data_dir()/'model-prices.json').get('models', {}).items():
        models.setdefault(provider, {}).update(items)
    return {'as_of': base.get('as_of'), 'models': models}


def cost(provider, model, usage):
    """USD for one call, or None when the model price or token counts are unknown."""
    if (provider in KEYLESS and provider != 'vertex') or (provider == 'openrouter' and model.endswith(':free')):
        return 0.0 if usage.get('input_tokens') is not None else None
    price = prices()['models'].get(provider, {}).get(model)
    if not price or usage.get('input_tokens') is None or usage.get('output_tokens') is None:
        return None
    try:
        cached = min(usage.get('cached_tokens') or 0, usage['input_tokens'])
        fresh = usage['input_tokens']-cached
        output = usage['output_tokens']+(usage.get('thinking_tokens') or 0)
        total = (fresh*price['input'] + cached*price.get('cached_input', price['input']) + output*price['output'])
    except (KeyError, TypeError):
        return None
    return total/1_000_000


# Calls ---------------------------------------------------------------------

def _route_gate(scope):
    """Called under _LOCK. Return a cooling error or a uniquely owned probe."""
    now = time.monotonic()
    quota_until = _COOLDOWNS.get(scope, 0)
    service = _SERVICE_CIRCUITS.get(scope, {})
    service_until = service.get('until', 0)
    # If both circuits exist, report the one whose interval ends last. The same
    # reason is retained while their single recovery probe is in flight.
    category = 'quota' if scope in _COOLDOWNS and quota_until >= service_until else 'service'
    until = max(quota_until, service_until)
    if until > now or scope in _ROUTE_PROBES:
        return ModelError(category, 'This model route is cooling down.', retry_after=max(0, until-now)), None
    if scope in _COOLDOWNS or service_until:
        token = object()
        _ROUTE_PROBES[scope] = token
        return None, token
    return None, None


def _route_outcome(scope, probe, error=None, *, sent=True):
    """Release only our probe; completed requests update shared route health."""
    with _LOCK:
        if probe is not None and _ROUTE_PROBES.get(scope) is probe:
            _ROUTE_PROBES.pop(scope)
        if not sent:
            return
        now = time.monotonic()
        if error is None:
            # An older in-flight success must not release another request's probe.
            if scope not in _ROUTE_PROBES:
                _COOLDOWNS.pop(scope, None)
                _SERVICE_CIRCUITS.pop(scope, None)
        elif error.category == 'quota':
            _COOLDOWNS[scope] = now + max(1, min(error.retry_after or 60, 3600))
        elif error.category == 'service':
            previous = _SERVICE_CIRCUITS.get(scope, {})
            count = previous.get('failures', 0) + 1 if now - previous.get('last', -float('inf')) <= SERVICE_FAILURE_WINDOW else 1
            # A failed half-open service probe reopens immediately.
            if probe is not None and previous.get('until'):
                count = max(count, SERVICE_FAILURE_THRESHOLD)
            _SERVICE_CIRCUITS[scope] = {'failures': count, 'last': now,
                'until': now + SERVICE_COOLDOWN if count >= SERVICE_FAILURE_THRESHOLD else 0}
        elif probe is not None:
            _COOLDOWNS.pop(scope, None)
            _SERVICE_CIRCUITS.pop(scope, None)


def complete(req, keys=None):
    """One end-to-end deadline; quota and repeated service failures cool routes.

    Skip cooling routes and use only explicitly configured fallbacks. With no
    eligible route return immediately so a durable task releases its worker.
    """
    keys = keys or {}
    deadline = time.monotonic() + req.total_timeout
    attempts, last = 0, None
    for position, (provider, model) in enumerate(targets(req)):
        if position and not usable(provider, keys):
            continue  # a fallback without its key cannot help; the primary route still decides the outcome
        if req.checkpoint:
            req.checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            error = ModelError('timeout', 'The total model request deadline elapsed.')
            error.attempts = attempts
            raise error
        scope = (str(data_dir()), provider, model, hashlib.sha256(keys.get(provider, '').encode()).hexdigest())
        with _LOCK:
            cooling, probe = _route_gate(scope)
            if cooling is not None:
                last = cooling
                last.attempts = attempts
                continue
        request = replace(req, total_timeout=remaining, idle_timeout=min(req.idle_timeout, remaining),
                          options=dict(req.options or route_options(req.stage, provider, model)))
        request._configured_timeout = req.total_timeout
        invocation = uuid.uuid4().hex
        sent = False
        try:
            if req.before_attempt:
                req.before_attempt(invocation)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelError('timeout', 'The total model request deadline elapsed during admission.')
            request.total_timeout = remaining
            request.idle_timeout = min(request.idle_timeout, remaining)
            attempts += 1
            sent = True
            try:
                result = _attempt(request, provider, model, keys)
            except ModelError as error:
                if req.after_attempt:
                    req.after_attempt(invocation, error.usage)
                raise
            if req.after_attempt:
                req.after_attempt(invocation, result.usage)
            _route_outcome(scope, probe)
            result.attempts = attempts
            return result
        except ModelError as error:
            error.attempts = attempts
            last = error
            _route_outcome(scope, probe, error, sent=sent)
            if error.category not in FALLBACK_CATEGORIES:
                raise
        except BaseException:
            _route_outcome(scope, probe, sent=False)
            raise
    if last is None:
        last = ModelError('config', 'No model route is configured.')
    last.attempts = attempts
    raise last


RATE_LIMIT_WAITS = (15, 30)
SLEEP = time.sleep


def _attempt(req, provider, model, keys, replay_of=None):
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    req._timings = {}
    req._raw = None
    raw, failure = None, None
    try:
        raw = _send(req, provider, model, keys)
        value = _finish(req, provider, raw)
    except ModelError as error:
        failure, value = error, None
        raw = raw or req._raw
    except Exception:
        failure, value = ModelError('service', 'The model call failed unexpectedly.'), None
        raw = raw or req._raw
    latency = int((time.monotonic()-started)*1000)
    usage = _usage(failure.usage if failure is not None and failure.usage else raw.usage if raw else {})
    amount = cost(provider, model, usage)
    _record(call_id, req, provider, model, raw, failure, usage, amount, latency, replay_of)
    if failure is not None:
        failure.call_id = call_id
        failure.usage = usage
        raise failure
    return ModelResult(text=raw.text, json=value, provider=provider, model=model,
                       model_version=raw.model_version, usage=usage, cost_usd=amount, latency_ms=latency,
                       ttft_ms=raw.ttft_ms, finish_reason=raw.finish_reason, call_id=call_id,
                       timings=dict(req._timings))


def _send(req, provider, model, keys):
    module = PROVIDERS.get(provider)
    if module is None or not isinstance(model, str) or not model:
        raise ModelError('config', f'No model is configured for stage {req.stage!r}. Check model routing.')
    if req.stage not in STAGES:
        raise ModelError('config', f'Unknown stage {req.stage!r}.')
    if not req.options:
        req.options = route_options(req.stage, provider, model)
    key = keys.get(provider) if provider not in KEYLESS else ''
    if provider not in KEYLESS and not key:
        raise ModelError('auth', f'Add a {NAMES[provider]} API key in Settings.')
    return module.call(req, model, key)


def salvage(text, schema):
    """Recover the complete leading items of a truncated JSON object whose main payload is an array.

    Keeps every array element that closed before the cut, then fills missing required top-level fields with
    empty values. Returns the parsed object, or None when nothing complete can be recovered.
    """
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        return None
    depth, in_string, escaped, last_cut = 0, False, False, None
    for index, char in enumerate(text):
        if in_string:
            escaped = not escaped and char == '\\'
            if char == '"' and not escaped:
                in_string = False
            elif char != '\\':
                escaped = False
            continue
        if char == '"':
            in_string = True
        elif char in '{[':
            depth += 1
        elif char in '}]':
            depth -= 1
            if depth == 2 and char == '}':
                last_cut = index + 1          # an element of a top-level array just closed
    if last_cut is None:
        return None
    head = text[:last_cut]
    try:
        value = json.loads(head + ']}')
    except ValueError:
        return None
    for key in schema.get('required', []):
        if key not in value:
            kind = (schema.get('properties', {}).get(key) or {}).get('type')
            value[key] = [] if kind == 'array' else 'Partial answer: the model reached its output limit.' if kind == 'string' else None
    return value


def _finish(req, provider, raw):
    name = NAMES[provider]
    if raw.finish_reason == 'length' and req.json_schema is not None:
        recovered = salvage(raw.text, req.json_schema)
        if recovered is not None:
            try:
                value = validate.coerce(recovered, req.json_schema)
                validate.check(value, req.json_schema)
                raw.finish_reason = 'length_salvaged'
                return value
            except ValueError:
                pass
    if raw.finish_reason == 'length':
        raise ModelError('output_limit', f'{name} reached its response token limit before completing the answer.', usage=raw.usage)
    if raw.finish_reason != 'stop' or not raw.text.strip():
        raise ModelError('invalid_output', f'{name} returned an incomplete or blocked response.', usage=raw.usage)
    if req.json_schema is None:
        try:
            return json.loads(raw.text)
        except ValueError:
            return None
    try:
        value = validate.coerce(json.loads(raw.text), req.json_schema)
        validate.check(value, req.json_schema)
    except ValueError as error:
        raise ModelError('invalid_output', f'{name} returned structured output that does not match the schema: {error}',
                         usage=raw.usage) from None
    return value


def _usage(raw):
    result = {'input_tokens': raw.get('input_tokens'), 'output_tokens': raw.get('output_tokens'),
              'thinking_tokens': raw.get('thinking_tokens') or 0, 'cached_tokens': raw.get('cached_tokens') or 0,
              'total_tokens': raw.get('total_tokens')}
    return result


def request_record(req, provider, model):
    return {'stage': req.stage, 'provider': provider, 'model': model, 'system': req.system, 'prompt': req.prompt,
            'json_schema': req.json_schema, 'images': [image_record(i) for i in req.images],
            'max_output_tokens': req.max_output_tokens, 'temperature': req.temperature,
            'prompt_version': req.prompt_version, 'idle_timeout': req.idle_timeout,
            'total_timeout': getattr(req, '_configured_timeout', req.total_timeout), 'metadata': req.metadata, 'options': req.options}


def _record(call_id, req, provider, model, raw, failure, usage, amount, latency, replay_of):
    from .store import canonical
    import hashlib
    request = request_record(req, provider, model)
    meta = req.metadata if isinstance(req.metadata, dict) else {}
    response = {'text': raw.text if raw else '', 'finish_reason': raw.finish_reason if raw else None,
                'model_version': raw.model_version if raw else None, 'usage': usage,
                'timings': dict(getattr(req, '_timings', {})),
                'error': {'category': failure.category, 'message': str(failure),
                          'provider_detail': getattr(failure, 'detail', None),
                          'partial': getattr(failure, 'partial', None)} if failure else None}
    row = {'call_id': call_id, 'provider': provider, 'model': model, 'stage': req.stage,
           'model_version': raw.model_version if raw else None, 'prompt_version': req.prompt_version,
           'input_hash': hashlib.sha256(canonical({k: request[k] for k in ('system', 'prompt', 'json_schema', 'images')})).hexdigest(),
           'input_chars': len(req.prompt)+len(req.system or ''), 'image_count': len(req.images),
           'status': 'error' if failure else 'ok', 'error_category': failure.category if failure else None,
           'input_tokens': usage['input_tokens'], 'output_tokens': usage['output_tokens'],
           'thinking_tokens': usage['thinking_tokens'], 'cached_tokens': usage['cached_tokens'],
           'cost_usd': amount, 'latency_ms': latency, 'ttft_ms': raw.ttft_ms if raw else ((getattr(failure, 'partial', None) or {}).get('ttft_ms')),
           'finish_reason': raw.finish_reason if raw else None, 'http_status': failure.status_code if failure else None,
           'collection_id': _text(meta.get('collection_id')), 'task_id': _text(meta.get('task_id')),
           'short_id': _text(meta.get('short_id')), 'replay_of': replay_of,
           **{name: getattr(req, '_timings', {}).get(name) for name in ('response_headers_ms', 'first_response_ms', 'first_content_ms')}}
    try:
        log().record(row, request, response)
    except Exception:
        # The call log must never turn a finished model call into a failure.
        pass


def _text(value):
    return None if value is None else str(value)


# Reading the log -----------------------------------------------------------

def calls(limit=50, **filters):
    return log().calls(limit, **filters)


def get_call(call_id):
    return log().get(call_id)


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered)*fraction)-1)] if ordered else None


def summary():
    rows = log().calls(1000000)
    groups = {}
    for row in rows:
        groups.setdefault((row['provider'], row['model'], row['stage']), []).append(row)
    result = []
    for (provider, model, stage), items in sorted(groups.items()):
        latencies = [r['latency_ms'] for r in items if r['latency_ms'] is not None]
        costs = [r['cost_usd'] for r in items if r['cost_usd'] is not None]
        result.append({'provider': provider, 'model': model, 'stage': stage, 'calls': len(items),
                       'failures': sum(r['status'] != 'ok' for r in items),
                       'p50_latency_ms': _percentile(latencies, .5), 'p95_latency_ms': _percentile(latencies, .95),
                       'input_tokens': sum(r['input_tokens'] or 0 for r in items),
                       'output_tokens': sum(r['output_tokens'] or 0 for r in items),
                       'thinking_tokens': sum(r['thinking_tokens'] or 0 for r in items),
                       'cached_tokens': sum(r['cached_tokens'] or 0 for r in items),
                       'cost_usd': sum(costs) if costs else None,
                       'cost_unknown_calls': len(items)-len(costs)})
    return result


def replay(call_id, provider, model, keys=None, images=None, options=None):
    """Re-send an archived request to another model; logged with replay_of=call_id."""
    row = log().get(call_id)
    if row is None or not row.get('request'):
        raise ModelError('config', 'That call is not in the log.')
    saved = row['request']
    images = images or []
    if [image_record(i)['sha256'] for i in images] != [i['sha256'] for i in saved.get('images', [])]:
        raise ModelError('config', 'This call used pictures that are not archived. Supply the original images to replay it.')
    if keys is None:
        from ..credentials import load_keys
        keys = load_keys(ROOT)
    req = ModelRequest(stage=saved['stage'], prompt=saved['prompt'], json_schema=saved.get('json_schema'),
                       images=images, max_output_tokens=saved['max_output_tokens'], provider=provider,
                       model=model, prompt_version=saved.get('prompt_version'), idle_timeout=saved['idle_timeout'],
                       total_timeout=saved['total_timeout'], temperature=saved.get('temperature'),
                       metadata=saved.get('metadata') or {}, system=saved.get('system'),
                       options=dict(options) if options is not None else {})
    return _attempt(req, provider, model, keys, replay_of=call_id)
