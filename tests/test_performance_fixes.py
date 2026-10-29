"""Offline concurrency/admission/deadline regressions; no provider access."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from clippie.collection_store import CollectionStore
from clippie.models import gateway, ModelRequest, ModelError


def task(storage, cid, key='one', calls=1, tokens=10):
    storage.enqueue_task(cid, 'map_source', 'v', key, reserved_calls=calls, reserved_tokens=tokens)
    return storage.claim_task(collection_id=cid)


def test_free_completion_after_overrun_and_proven_cache_release(tmp_path):
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'], budget={'calls': 1, 'tokens': 10})['id']
    paid = task(storage, cid)
    storage.finish_task(paid['id'], paid['lease_token'], 'v', actual_calls=2, actual_tokens=20)
    free = task(storage, cid, 'free', 0, 0)
    assert free is not None
    storage.finish_task(free['id'], free['lease_token'], 'v', actual_calls=0, actual_tokens=0)
    assert storage.get_collection(cid)['usage'] == {'calls': 2, 'tokens': 20}
    cid = storage.create_collection(['s'], budget={'calls': 1, 'tokens': 10})['id']
    cached = task(storage, cid)
    storage.finish_task(cached['id'], cached['lease_token'], 'v', actual_calls=0, actual_tokens=0)
    assert storage.get_collection(cid)['usage'] == {'calls': 0, 'tokens': 0}
    storage.record_usage(cached['id'], cached['lease_token'], actual_calls=0, actual_tokens=0)
    assert storage.get_collection(cid)['usage'] == {'calls': 0, 'tokens': 0}


def test_cross_instance_admission_is_atomic_additive_and_idempotent(tmp_path):
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'], budget={'calls': 2, 'tokens': 20})['id']
    claimed = task(storage, cid)
    storage.admit_invocation(claimed['id'], claimed['lease_token'], 'first', 10)
    barrier = threading.Barrier(2)
    def admit(identity):
        other = CollectionStore(tmp_path)
        barrier.wait()
        try:
            other.admit_invocation(claimed['id'], claimed['lease_token'], identity, 10)
            return identity
        except ValueError:
            return None
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(admit, ['second', 'third']))
    accepted = next(x for x in ids if x)
    assert sum(x is not None for x in ids) == 1
    for identity in ['first', accepted, accepted]:
        storage.reconcile_invocation(claimed['id'], claimed['lease_token'], identity, 3)
    attempt = storage.list_attempts(claimed['id'])[0]
    assert attempt['reported_usage'] == {'calls': 2, 'tokens': 6}
    assert storage.get_collection(cid)['usage'] == {'calls': 2, 'tokens': 6}
    storage.cancel_collection(cid)
    with pytest.raises(ValueError):
        storage.admit_invocation(claimed['id'], claimed['lease_token'], 'cancelled', 10)


def test_unknown_expired_invocation_keeps_reservation(tmp_path):
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'], budget={'calls': 2, 'tokens': 20})['id']
    claimed = task(storage, cid)
    storage.admit_invocation(claimed['id'], claimed['lease_token'], 'sent', 10)
    storage.recover_expired(now=claimed['lease_until'] + 1)
    assert storage.get_collection(cid)['usage'] == {'calls': 1, 'tokens': 10}
    assert storage.list_attempts(claimed['id'])[0]['reported_usage']['tokens'] is None


def test_fallback_receives_only_remaining_deadline_and_route_options(monkeypatch):
    clock = [0.]
    seen = []
    monkeypatch.setattr(gateway.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(gateway, 'targets', lambda req: [('gemini', 'a'), ('openai', 'b')])
    monkeypatch.setattr(gateway, 'route_options', lambda stage, provider, model: {'route': provider})
    def attempt(req, provider, model, keys):
        seen.append((req.total_timeout, req.options))
        if provider == 'gemini':
            clock[0] += 7
            raise ModelError('timeout', 'timeout')
        return SimpleNamespace(attempts=0, usage={})
    monkeypatch.setattr(gateway, '_attempt', attempt)
    gateway.complete(ModelRequest(total_timeout=10), {'gemini': 'g-key', 'openai': 'o-key'})
    assert seen == [(10, {'route': 'gemini'}), (3, {'route': 'openai'})]


def test_quota_cooldown_shared_by_stage_but_not_model_and_cancel_checkpoint(monkeypatch):
    seen = []
    def attempt(req, provider, model, keys):
        seen.append(model)
        raise ModelError('quota', 'quota', retry_after=30)
    monkeypatch.setattr(gateway, '_attempt', attempt)
    for stage in ['map', 'review']:
        with pytest.raises(ModelError):
            gateway.complete(ModelRequest(stage=stage, provider='gemini', model='same'), {'gemini': 'x'})
    assert seen == ['same']
    with pytest.raises(ModelError):
        gateway.complete(ModelRequest(provider='gemini', model='other'), {'gemini': 'x'})
    assert seen == ['same', 'other']
    def cancelled():
        raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        gateway.complete(ModelRequest(provider='gemini', model='same', checkpoint=cancelled), {'gemini': 'x'})
    assert seen == ['same', 'other']


def test_discovery_checkpoint_survives_retry_and_cancel_fences_writes(tmp_path):
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'], budget={'calls': 3, 'tokens': 30})['id']
    claimed = task(storage, cid)
    response = {'proposal': {'candidates': []}, 'usage': {'total_tokens': 2}}
    storage.save_stage_checkpoint(claimed['id'], claimed['lease_token'], 'window1', response)
    storage.finish_task(claimed['id'], claimed['lease_token'], 'v', error='provider_timeout')
    storage.retry_task(claimed['id'])
    retried = storage.claim_task(collection_id=cid)
    assert CollectionStore(tmp_path).get_stage_checkpoint(claimed['id'], 'window1') == response
    storage.cancel_collection(cid)
    with pytest.raises(ValueError):
        storage.save_stage_checkpoint(retried['id'], retried['lease_token'], 'window2', response)


def test_two_remote_and_one_local_task_overlap_with_duplicate_graph_owner_fenced(tmp_path, monkeypatch):
    from clippie import collection_generation as generation, collection_workflow
    monkeypatch.setattr(collection_workflow, 'schedule_recheck', lambda *args: None)
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'])['id']
    run = storage.start_run(cid, {}, 'run')
    storage.enqueue_task(cid, 'discover', 'v', 'discover', payload={'run_id': run['id']})
    first = storage.claim_task(collection_id=cid)
    storage.finish_task(first['id'], first['lease_token'], 'v', result={})
    for stage, identity in [('map_source', 'a'), ('map_source', 'b'), ('compile_short', 'c')]:
        storage.enqueue_task(cid, stage, 'v', identity, payload={'run_id': run['id']})
    reached = threading.Barrier(3)
    release = threading.Event()
    started = threading.Event()
    seen = []
    def handler(ctx):
        seen.append(ctx.task['id'])
        reached.wait(timeout=3)
        started.set()
        assert release.wait(3)
        return {'result': {}}
    monkeypatch.setattr(generation, '_handlers', lambda *args: {'map_source': handler, 'compile_short': handler})
    monkeypatch.setattr(generation, '_advance', lambda *args: None)
    owner = threading.Thread(target=generation.drain_generation, args=(storage, None, cid, None))
    owner.start()
    assert started.wait(3)
    # Direct batch/coordinator duplicates do not advance or run the same graph.
    generation.drain_generation(CollectionStore(tmp_path), None, cid, None)
    release.set()
    owner.join(4)
    assert not owner.is_alive()
    assert len(seen) == len(set(seen)) == 3
    assert all(t['status'] == 'completed' for t in storage.list_tasks(cid))


def test_legacy_cache_reconciliation_is_bounded_to_completed_proven_zero(tmp_path):
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'], budget={'calls': 3, 'tokens': 30})['id']
    cached = task(storage, cid)
    # Historical format: success with zero reported usage but full reservation retained.
    storage.finish_task(cached['id'], cached['lease_token'], 'v', result={})
    with storage._transaction() as db:
        attempt = storage._attempt(db, cached['id'], cached['lease_token'])
        attempt['reported_usage'] = {'tokens': 0, 'calls': 0}
        storage._save(db, 'attempt', attempt)
    unknown = task(storage, cid, 'unknown')
    storage.recover_expired(now=unknown['lease_until'] + 1)
    assert storage.reconcile_no_request_attempts(cid) == {'tokens': 10, 'calls': 1}
    assert storage.reconcile_no_request_attempts(cid) == {'tokens': 0, 'calls': 0}
    assert storage.get_collection(cid)['usage'] == {'tokens': 10, 'calls': 1}


def test_queue_does_not_consume_execution_allowance_and_restart_does_not_extend_it(tmp_path, monkeypatch):
    from clippie import collection_store
    now = [100.]
    monkeypatch.setattr(collection_store.time, 'time', lambda: now[0])
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'])['id']
    run = storage.start_run(cid, {'seconds': 30, 'deadline': 130}, 'r')
    now[0] = 500.
    started = storage.start_execution_clock(run['id'])
    assert started['execution_started_at'] == 500 and started['deadline'] == 530
    now[0] = 600.
    assert CollectionStore(tmp_path).start_execution_clock(run['id'])['deadline'] == 530


def test_physical_fallback_is_refused_before_transport_when_allowance_is_spent(tmp_path, monkeypatch):
    import time
    import httpx
    from clippie import app, collection_provider
    from clippie.collection_runner import StageContext
    from tests.support_models import install
    storage = CollectionStore(tmp_path)
    cap = collection_provider.RESERVATION_TOKENS
    cid = storage.create_collection(['s'], budget={'calls': 1, 'tokens': cap})['id']
    storage.enqueue_task(cid, 'plan_short', 'v', 'paid', reserved_calls=1, reserved_tokens=cap,
                         payload={'deadline': time.time() + 60})
    claimed = storage.claim_task(collection_id=cid)
    gateway.save_routing({'defaults': {'plan': {'provider': 'gemini', 'model': 'primary'}},
                         'fallbacks': {'plan': [{'provider': 'anthropic', 'model': 'fallback'}]}})
    monkeypatch.setattr(app, 'KEYS', {'gemini': 'fake', 'anthropic': 'fake'})
    seen = []
    install(monkeypatch, lambda req: seen.append(req) or httpx.Response(429))
    ctx = StageContext(storage, claimed, 60)
    with pytest.raises(collection_provider.CollectionProviderError, match='provider_retry_budget'):
        collection_provider.invoke(ctx, 'fake', 'plan', 'evidence', {'type': 'object'}, 100)
    assert len(seen) == 1
    assert storage.list_attempts(claimed['id'])[0]['reported_usage']['calls'] == 1
    assert storage.get_collection(cid)['usage']['calls'] == 1


def test_discovery_wrapper_replays_window_without_a_second_invocation(tmp_path, monkeypatch):
    import time
    from clippie import app, collection_generation as generation
    from clippie.collection_runner import StageContext
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'], budget={'calls': 3, 'tokens': 30})['id']
    storage.enqueue_task(cid, 'discover', 'v', 'discovery', reserved_calls=1, reserved_tokens=10,
                         payload={'deadline': time.time() + 60})
    claimed = storage.claim_task(collection_id=cid)
    calls = []
    monkeypatch.setattr(app, 'KEYS', {'gemini': 'fake'})
    def invoke(*args, **kwargs):
        calls.append(args)
        return {'proposal': {'candidates': []}, 'usage': {'total_tokens': 7}, 'provider_requests': 1}
    monkeypatch.setattr(generation.provider, 'invoke', invoke)
    ctx = StageContext(storage, claimed, 60)
    first = generation._invoke(ctx)('discover', 'window with fixed already_proposed', {}, 100)
    storage.finish_task(claimed['id'], claimed['lease_token'], 'v', error='provider_timeout')
    storage.retry_task(claimed['id'])
    retried = storage.claim_task(collection_id=cid)
    resumed = StageContext(CollectionStore(tmp_path), retried, 60)
    reused = generation._invoke(resumed)('discover', 'window with fixed already_proposed', {}, 100)
    assert reused['proposal'] == first['proposal'] and reused['usage'] == {} and reused['provider_requests'] == 0
    assert len(calls) == 1
    generation._invoke(resumed)('discover', 'next window with changed already_proposed', {}, 100)
    assert len(calls) == 2


def test_zero_reservation_cannot_authorize_an_unbudgeted_physical_call(tmp_path):
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'])['id']
    free = task(storage, cid, calls=0, tokens=0)
    with pytest.raises(ValueError, match='invocation_budget_exhausted'):
        storage.admit_invocation(free['id'], free['lease_token'], 'unbudgeted', 10)
    assert storage.list_attempts(free['id'])[0]['charged_usage'] == {'calls': 0, 'tokens': 0}


def test_interrupted_admission_remains_unknown_after_task_failure(tmp_path):
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['s'], budget={'calls': 1, 'tokens': 10})['id']
    claimed = task(storage, cid)
    storage.admit_invocation(claimed['id'], claimed['lease_token'], 'unknown', 10)
    storage.finish_task(claimed['id'], claimed['lease_token'], 'v', error='interrupted')
    report = storage.usage_report(cid)
    assert report['unknown_call_attempts'] == report['unknown_token_attempts'] == 1
    assert storage.get_collection(cid)['usage'] == {'calls': 1, 'tokens': 10}
