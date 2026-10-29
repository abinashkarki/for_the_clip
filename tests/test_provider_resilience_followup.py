"""Offline route outage/recovery and client-observed transport timing regressions."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from clippie.models import gateway, ModelError, ModelRequest
from clippie.models.store import CallLog, COLUMNS, INTEGERS
from tests.support_models import install, gemini_stream, sse


@pytest.fixture
def circuit(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(gateway.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(gateway, '_COOLDOWNS', {})
    monkeypatch.setattr(gateway, '_SERVICE_CIRCUITS', {})
    monkeypatch.setattr(gateway, '_ROUTE_PROBES', {})
    monkeypatch.setattr(gateway, 'route_options', lambda *args: {})
    return clock


def route(stage='discover', **kwargs):
    return ModelRequest(stage=stage, provider='gemini', model='primary', **kwargs)


def failed(monkeypatch):
    seen = []
    def attempt(req, provider, model, keys):
        seen.append((req.stage, provider, model, keys.get(provider)))
        raise ModelError('service', 'temporary outage')
    monkeypatch.setattr(gateway, '_attempt', attempt)
    return seen


def open_service(monkeypatch):
    seen = failed(monkeypatch)
    for stage in ('discover', 'review'):
        with pytest.raises(ModelError, match='temporary outage'):
            gateway.complete(route(stage), {'gemini': 'one'})
    return seen


def test_service_circuit_shared_across_stages_and_only_configured_fallback(monkeypatch, circuit):
    seen = open_service(monkeypatch)
    with pytest.raises(ModelError) as error:
        gateway.complete(route('repair'), {'gemini': 'one'})
    assert error.value.category == 'service' and error.value.attempts == 0
    assert error.value.retry_after == 30 and len(seen) == 2
    monkeypatch.setattr(gateway, 'targets', lambda req: [('gemini', 'primary'), ('nvidia', 'fallback')])
    monkeypatch.setattr(gateway, '_attempt', lambda req, provider, model, keys:
                        seen.append((req.stage, provider)) or SimpleNamespace(usage={}))
    with pytest.raises(ModelError):
        gateway.complete(ModelRequest(stage='repair'), {'gemini': 'one'})
    assert gateway.complete(ModelRequest(stage='repair'), {'gemini': 'one', 'nvidia': 'test-key'}).attempts == 1
    assert seen[-1] == ('repair', 'nvidia')


def test_single_failure_expires_and_success_resets_sequence(monkeypatch, circuit):
    seen = failed(monkeypatch)
    for delta in (0, gateway.SERVICE_FAILURE_WINDOW + 1):
        circuit[0] += delta
        with pytest.raises(ModelError, match='temporary outage'):
            gateway.complete(route(), {'gemini': 'one'})
    assert len(seen) == 2  # Two distant failures do not open the circuit.
    monkeypatch.setattr(gateway, '_attempt', lambda *args, **kwargs: SimpleNamespace(usage={}))
    gateway.complete(route(), {'gemini': 'one'})
    assert gateway._SERVICE_CIRCUITS == {}
    failed(monkeypatch)
    with pytest.raises(ModelError, match='temporary outage'):
        gateway.complete(route(), {'gemini': 'one'})


def test_service_scope_separates_credentials_models_and_workspaces(monkeypatch, circuit, tmp_path):
    seen = open_service(monkeypatch)
    for req, keys in ((route(), {'gemini': 'two'}),
                      (ModelRequest(provider='gemini', model='other'), {'gemini': 'one'})):
        with pytest.raises(ModelError, match='temporary outage'):
            gateway.complete(req, keys)
    old = gateway.data_dir()
    gateway.configure(tmp_path / 'another-workspace')
    try:
        with pytest.raises(ModelError, match='temporary outage'):
            gateway.complete(route(), {'gemini': 'one'})
    finally:
        gateway.configure(old)
    assert len(seen) == 5


def test_half_open_probe_held_until_finished_even_after_its_deadline(monkeypatch, circuit):
    open_service(monkeypatch)
    circuit[0] += gateway.SERVICE_COOLDOWN + 1
    reached, release = threading.Event(), threading.Event()
    def probe(*args):
        reached.set()
        assert release.wait(3)
        return SimpleNamespace(usage={})
    monkeypatch.setattr(gateway, '_attempt', probe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gateway.complete, route(total_timeout=10), {'gemini': 'one'})
        assert reached.wait(3)
        circuit[0] += 1000
        with pytest.raises(ModelError) as error:
            gateway.complete(route(), {'gemini': 'one'})
        assert error.value.category == 'service' and error.value.attempts == 0
        release.set()
        future.result(timeout=3)
    assert gateway._ROUTE_PROBES == {} and gateway._SERVICE_CIRCUITS == {}


def test_failed_probe_reopens_and_admission_cancellation_releases_probe(monkeypatch, circuit):
    seen = open_service(monkeypatch)
    circuit[0] += gateway.SERVICE_COOLDOWN + 1
    def cancelled(identity):
        raise RuntimeError('cancelled before transport')
    with pytest.raises(RuntimeError, match='cancelled'):
        gateway.complete(route(before_attempt=cancelled), {'gemini': 'one'})
    assert not gateway._ROUTE_PROBES and len(seen) == 2
    with pytest.raises(ModelError, match='temporary outage'):
        gateway.complete(route(), {'gemini': 'one'})
    assert len(seen) == 3 and not gateway._ROUTE_PROBES
    with pytest.raises(ModelError, match='cooling down'):
        gateway.complete(route(), {'gemini': 'one'})
    assert len(seen) == 3


def test_quota_retains_distinct_category_and_retry_policy(monkeypatch, circuit):
    def attempt(*args):
        raise ModelError('quota', 'quota', retry_after=75)
    monkeypatch.setattr(gateway, '_attempt', attempt)
    with pytest.raises(ModelError):
        gateway.complete(route(), {'gemini': 'one'})
    with pytest.raises(ModelError) as error:
        gateway.complete(route('review'), {'gemini': 'one'})
    assert error.value.category == 'quota' and error.value.retry_after == 75
    assert not gateway._SERVICE_CIRCUITS


def test_overlapping_quota_and_service_circuits_use_longer_cooldown_and_one_probe(monkeypatch, circuit):
    seen = open_service(monkeypatch)
    scope = next(iter(gateway._SERVICE_CIRCUITS))
    # An already-in-flight request can return quota after siblings open service.
    gateway._route_outcome(scope, None, ModelError('quota', 'quota', retry_after=75))
    with pytest.raises(ModelError) as error:
        gateway.complete(route(), {'gemini': 'one'})
    assert error.value.category == 'quota' and error.value.retry_after == 75
    circuit[0] += 76
    def refused(identity):
        raise ValueError('admission refused')
    with pytest.raises(ValueError, match='admission refused'):
        gateway.complete(route(before_attempt=refused), {'gemini': 'one'})
    assert not gateway._ROUTE_PROBES and len(seen) == 2
    # The declined probe did not extend either interval or consume a transport.
    with pytest.raises(ModelError, match='temporary outage'):
        gateway.complete(route(), {'gemini': 'one'})
    assert len(seen) == 3
    with pytest.raises(ModelError) as error:
        gateway.complete(route(), {'gemini': 'one'})
    assert error.value.category == 'service' and error.value.retry_after == 30


def test_transport_timing_distinguishes_headers_line_and_content(monkeypatch):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b': keepalive\n\n'
            time.sleep(.02)
            yield sse({'choices': [{'delta': {'reasoning': 'hidden'}}]}).encode()
            time.sleep(.02)
            yield sse({'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}).encode()
    install(monkeypatch, lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Chunks()))
    result = gateway.complete(ModelRequest(provider='nvidia', model='test'), {'nvidia': 'fake'})
    row = gateway.get_call(result.call_id)
    assert 0 <= row['response_headers_ms'] <= row['first_response_ms'] < row['first_content_ms']
    assert row['first_content_ms'] >= 20
    assert result.timings == row['response']['timings']
    assert row['ttft_ms'] is not None and 'first_byte_ms' not in result.timings


def test_http_error_and_schema_failure_preserve_available_timings_and_usage(monkeypatch):
    install(monkeypatch, lambda req: httpx.Response(503))
    with pytest.raises(ModelError) as error:
        gateway.complete(route(), {'gemini': 'fake'})
    row = gateway.get_call(error.value.call_id)
    assert row['response_headers_ms'] is not None and row['first_content_ms'] is None
    assert row['response']['timings']['transport_started_at'] > 0
    install(monkeypatch, lambda req: gemini_stream('{"bad":true}'))
    req = route(json_schema={'type': 'object', 'required': ['ok']})
    with pytest.raises(ModelError) as error:
        gateway.complete(req, {'gemini': 'fake'})
    row = gateway.get_call(error.value.call_id)
    assert error.value.category == 'invalid_output' and error.value.usage['total_tokens'] == 150
    assert row['first_content_ms'] is not None and row['input_tokens'] == 100


def test_partial_network_error_retains_observed_content_timing_and_usage(monkeypatch):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield sse({'candidates': [{'content': {'parts': [{'text': '{'}]}}],
                       'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 1, 'totalTokenCount': 11}}).encode()
            raise httpx.ReadError('private transport error')
    install(monkeypatch, lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Broken()))
    with pytest.raises(ModelError) as error:
        gateway.complete(route(), {'gemini': 'fake'})
    row = gateway.get_call(error.value.call_id)
    assert error.value.category == 'network' and error.value.usage['total_tokens'] == 11
    assert row['first_content_ms'] is not None and row['ttft_ms'] is not None


def test_existing_call_log_migrates_additively(tmp_path):
    db = sqlite3.connect(tmp_path / 'models.sqlite3')
    original = [column for column in COLUMNS if column not in {'response_headers_ms', 'first_response_ms', 'first_content_ms'}]
    db.execute('CREATE TABLE model_calls (' + ', '.join(
        f'{c} {"INTEGER" if c in INTEGERS else "REAL" if c in {"created_at", "cost_usd"} else "TEXT"}'
        + (' PRIMARY KEY' if c == 'call_id' else '') for c in original) + ')')
    db.execute("INSERT INTO model_calls (call_id,created_at,stage,provider) VALUES ('old',1,'map','gemini')")
    db.commit()
    db.close()
    log = CallLog(tmp_path)
    row = log.calls()[0]
    assert row['call_id'] == 'old' and row['response_headers_ms'] is None and row['first_content_ms'] is None
