"""Per-request settlement and bounded fallback admission, entirely offline."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from clippie.collection_store import CollectionStore
from clippie import collection_generation as generation
from tests.test_generation_allowance import long_source


def claimed(store, cid, key, tokens=20, calls=1):
    store.enqueue_task(cid, 'repair_batch', 'v', key,
                       reserved_tokens=tokens, reserved_calls=calls)
    return store.claim_task(collection_id=cid)


def admit(store, task, identity, tokens=20):
    return store.admit_invocation(task['id'], task['lease_token'], identity, tokens)


def settle(store, task, identity, tokens=None):
    return store.reconcile_invocation(task['id'], task['lease_token'], identity, tokens)


def test_mixed_unknown_and_known_requests_settle_independently_and_idempotently(tmp_path):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 60, 'calls': 3})['id']
    task = claimed(store, cid, 'batch')
    admit(store, task, 'failed')
    settle(store, task, 'failed')  # terminal, but missing tokens does not imply free
    admit(store, task, 'fallback')
    settle(store, task, 'fallback', 3)
    assert store.get_collection(cid)['usage'] == {'tokens': 23, 'calls': 2}
    assert store.list_attempts(task['id'])[0]['reported_usage']['tokens'] is None
    settle(store, task, 'fallback', 3)
    settle(store, task, 'fallback', 1)
    assert store.get_collection(cid)['usage'] == {'tokens': 23, 'calls': 2}
    # A later complete usage report can settle the previously unknown request.
    settle(store, task, 'failed', 0)
    assert store.get_collection(cid)['usage'] == {'tokens': 3, 'calls': 2}


@pytest.mark.parametrize('end', ['failed', 'cancelled', 'expired'])
def test_late_known_excess_counts_without_promoting_a_stopped_result(tmp_path, end):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 20, 'calls': 1})['id']
    task = claimed(store, cid, 'request')
    admit(store, task, 'sent')
    if end == 'failed':
        store.finish_task(task['id'], task['lease_token'], 'v', error='provider_service')
    elif end == 'cancelled':
        store.cancel_collection(cid)
    else:
        store.recover_expired(now=task['lease_until'] + 1)
    state = store.list_tasks(cid)[0]['status']
    settle(store, task, 'sent', 3)
    assert store.get_collection(cid)['usage'] == {'tokens': 3, 'calls': 1}
    settle(store, task, 'sent', 25)
    settle(store, task, 'sent', 2)
    assert store.get_collection(cid)['usage'] == {'tokens': 25, 'calls': 1}
    assert store.list_tasks(cid)[0]['status'] == state
    assert store.list_tasks(cid)[0].get('result') is None


def test_legacy_ledger_without_envelopes_does_not_refund_known_usage(tmp_path):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 40, 'calls': 2})['id']
    task = claimed(store, cid, 'legacy')
    admit(store, task, 'sent')
    with store._transaction() as db:
        attempt = store._attempt(db, task['id'], task['lease_token'])
        attempt.pop('invocation_accounting_version')
        attempt['invocations']['sent'].pop('reservation')
        attempt['invocations']['sent'].pop('terminal')
        store._save(db, 'attempt', attempt)
    settle(store, task, 'sent', 3)
    assert store.get_collection(cid)['usage'] == {'tokens': 20, 'calls': 1}


def test_late_aggregate_excess_is_a_floor_without_settling_unknown_siblings(tmp_path):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 40, 'calls': 2})['id']
    task = claimed(store, cid, 'batch')
    admit(store, task, 'first')
    settle(store, task, 'first', 3)
    admit(store, task, 'unknown')
    store.cancel_collection(cid)
    store.record_usage(task['id'], task['lease_token'], actual_tokens=50, actual_calls=3)
    assert store.get_collection(cid)['usage'] == {'tokens': 50, 'calls': 3}
    settle(store, task, 'unknown', 2)
    store.record_usage(task['id'], task['lease_token'], actual_tokens=1, actual_calls=1)
    assert store.get_collection(cid)['usage'] == {'tokens': 50, 'calls': 3}
    assert store.list_tasks(cid)[0]['status'] == 'cancelled'


def test_settled_allowance_can_be_reused_by_only_one_concurrent_admission(tmp_path):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 25, 'calls': 5})['id']
    task = claimed(store, cid, 'request')
    admit(store, task, 'first')
    settle(store, task, 'first', 3)
    barrier = threading.Barrier(2)
    def request(identity):
        local = CollectionStore(tmp_path)
        barrier.wait()
        try:
            admit(local, task, identity)
        except ValueError as error:
            assert str(error) == 'invocation_budget_exhausted'
            return False
        return True
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(request, ['second', 'third']))
    assert sum(results) == 1
    assert store.get_collection(cid)['usage'] == {'tokens': 23, 'calls': 2}


def test_invocation_budget_diagnostic_is_persisted_and_cleared_on_retry(tmp_path):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 25, 'calls': 4})['id']
    task = claimed(store, cid, 'request')
    admit(store, task, 'unknown')
    with pytest.raises(ValueError, match='invocation_budget_exhausted'):
        admit(store, task, 'fallback')
    blocked = store.list_tasks(cid)[0]['budget_block']
    assert blocked == {'dimensions': ['tokens'], 'required': {'tokens': 20, 'calls': 1},
                       'remaining': {'tokens': 5, 'calls': 3}}
    assert len(store.list_attempts(task['id'])[0]['invocations']) == 1
    store.finish_task(task['id'], task['lease_token'], 'v', error='provider_retry_budget')
    store.retry_task(task['id'])
    assert 'budget_block' not in store.list_tasks(cid)[0]


def test_claim_budget_diagnostic_distinguishes_both_dimensions(tmp_path):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 1, 'calls': 0})['id']
    assert claimed(store, cid, 'request') is None
    assert store.list_tasks(cid)[0]['budget_block']['dimensions'] == ['tokens', 'calls']


def test_synthetic_original_allowance_replay_finishes_service_fallback_path(tmp_path):
    # Maps/discovery/first-review figures are archived measurements. The final
    # repair/re-review values below are injected, not observed provider results.
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': 933888, 'calls': 16})['id']
    for index, usage in enumerate([7308, 8563, 8056, 7357, 5055]):
        task = claimed(store, cid, f'map{index}', tokens=28672)
        admit(store, task, f'map{index}', 28672)
        settle(store, task, f'map{index}', usage)
        store.finish_task(task['id'], task['lease_token'], 'v', result={})
    for stage, usage in [('discover', 32947), ('review', 2191), ('repair', 3000)]:
        task = claimed(store, cid, stage, tokens=147456)
        assert task is not None
        admit(store, task, stage + '-primary', 147456)
        settle(store, task, stage + '-primary')
        admit(store, task, stage + '-fallback', 147456)
        settle(store, task, stage + '-fallback', usage)
        store.finish_task(task['id'], task['lease_token'], 'v', result={})
    task = claimed(store, cid, 'review-again', tokens=147456)
    assert task is not None
    admit(store, task, 'review-again', 147456)
    settle(store, task, 'review-again', 1000)
    store.finish_task(task['id'], task['lease_token'], 'v', result={})
    assert store.get_collection(cid)['usage'] == {'tokens': 517845, 'calls': 12}


def test_schema_validation_failure_settles_known_usage_without_promoting(tmp_path, monkeypatch):
    import time
    from clippie import app, collection_provider
    from clippie.collection_runner import StageContext
    from clippie.models import gateway
    from tests.support_models import install, gemini_stream

    cap = collection_provider.RESERVATION_TOKENS
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'], budget={'tokens': cap, 'calls': 1})['id']
    store.enqueue_task(cid, 'plan_short', 'v', 'invalid', reserved_tokens=cap, reserved_calls=1,
                       payload={'deadline': time.time() + 60})
    task = store.claim_task(collection_id=cid)
    gateway.save_routing({'defaults': {'plan': {'provider': 'gemini', 'model': 'fake'}},
                          'fallbacks': {'plan': []}})
    monkeypatch.setattr(app, 'KEYS', {'gemini': 'fake'})
    install(monkeypatch, lambda request: gemini_stream('{"ok":true}'))
    with pytest.raises(collection_provider.CollectionProviderError, match='provider_invalid_output'):
        collection_provider.invoke(StageContext(store, task, 60), 'fake', 'plan', 'evidence',
            {'type': 'object', 'properties': {'ok': {'type': 'integer'}}, 'required': ['ok']}, 100)
    assert store.get_collection(cid)['usage'] == {'tokens': 150, 'calls': 1}
    store.finish_task(task['id'], task['lease_token'], 'v', error='provider_invalid_output')
    assert store.list_tasks(cid)[0].get('result') is None
    assert store.get_collection(cid)['usage'] == {'tokens': 150, 'calls': 1}


def test_default_recovery_slots_use_volume_token_envelopes(tmp_path, monkeypatch):
    sources, prepared = long_source(monkeypatch)
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['s'])['id']
    run = generation.start_generation(store, sources, cid, None, dispatch_work=False)
    map_tokens = sum(generation._map_reservation(p) for p in prepared['artifact']['packets'])
    # Sixteen maps, four discovery windows, the opening judge, three normal review/repair stages,
    # three spare requests. Every spare has a volume-sized matching allowance.
    assert run['configuration']['calls'] == 27
    assert store.get_collection(cid)['budget']['tokens'] == map_tokens + 11 * generation.provider.VOLUME_RESERVATION_TOKENS
