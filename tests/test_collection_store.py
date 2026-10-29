import pytest
from clippie.collection_store import CollectionStore


def test_durable_revisions_and_independent_siblings(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    a = store.add_short(collection['id'])
    b = store.add_short(collection['id'])
    first = store.append_revision(a['id'], {'title': 'first'})
    with pytest.raises(ValueError, match='changed'):
        store.append_revision(a['id'], {'title': 'stale'})
    store.append_revision(a['id'], {'title': 'second'}, first['id'])
    reopened = CollectionStore(tmp_path)
    assert reopened.get_revision(first['id'])['document'] == {'title': 'first'}
    assert reopened.get_short(b['id'])['current_revision'] is None
    assert len(reopened.list_revisions(a['id'])) == 2


def test_duplicate_enqueue_and_dependency(tmp_path):
    store = CollectionStore(tmp_path)
    c = store.create_collection(['source'])['id']
    first = store.enqueue_task(c, 'prepare', 'v1', 'prepare-v1')
    assert CollectionStore(tmp_path).enqueue_task(c, 'prepare', 'v1', 'prepare-v1')['id'] == first['id']
    second = store.enqueue_task(c, 'plan', 'v1', 'plan-v1', dependencies=[first['id']])
    claim = store.claim_task(now=1)
    assert claim['id'] == first['id']
    assert store.claim_task(now=2) is None
    store.finish_task(first['id'], claim['lease_token'], 'v1', now=2)
    assert store.claim_task(now=3)['id'] == second['id']


def test_cross_instance_expiry_and_attempt_cap(tmp_path):
    a, b = CollectionStore(tmp_path), CollectionStore(tmp_path)
    c = a.create_collection(['source'])['id']
    t = a.enqueue_task(c, 'prepare', 'v1', 'key', max_attempts=2)
    old = a.claim_task(now=10, lease_seconds=2)
    assert b.claim_task(now=11) is None
    new = b.claim_task(now=12, lease_seconds=2)
    assert new['lease_token'] != old['lease_token']
    with pytest.raises(ValueError, match='Stale'):
        a.finish_task(t['id'], old['lease_token'], 'v1', now=13)
    assert a.recover_expired(now=14)[0]['status'] == 'failed'
    with pytest.raises(ValueError, match='retried'):
        b.retry_task(t['id'])
    assert b.claim_task(now=15) is None


def test_cancel_fences_results_and_revision_but_preserves_other_collection(tmp_path):
    a, b = CollectionStore(tmp_path), CollectionStore(tmp_path)
    c = a.create_collection(['source'])['id']
    short = a.add_short(c)
    revision = a.append_revision(short['id'], {'title': 'good'})
    t = a.enqueue_task(c, 'plan', 'v1', 'key', short_id=short['id'])
    claim = a.claim_task()
    other = a.create_collection(['other'])['id']
    other_task = a.enqueue_task(other, 'prepare', 'v1', 'key')
    b.cancel_collection(c)
    with pytest.raises(ValueError, match='cancelled'):
        a.finish_task(t['id'], claim['lease_token'], 'v1')
    with pytest.raises(ValueError, match='cancelled'):
        a.append_revision(short['id'], {}, revision['id'], task_id=t['id'], lease_token=claim['lease_token'], input_version='v1')
    assert a.get_revision(revision['id'])['document']['title'] == 'good'
    assert a.claim_task()['id'] == other_task['id']


def test_reservations_hold_and_retries_charge_again(tmp_path):
    a = CollectionStore(tmp_path)
    c = a.create_collection(['source'], budget={'tokens': 10, 'calls': 2})['id']
    t = a.enqueue_task(c, 'plan', 'v1', 'key', reserved_tokens=6, reserved_calls=1)
    claim = a.claim_task(now=1)
    assert a.get_collection(c)['usage'] == {'tokens': 6, 'calls': 1}
    a.finish_task(t['id'], claim['lease_token'], 'v1', error='transient', now=2)
    a.retry_task(t['id'])
    assert CollectionStore(tmp_path).claim_task(now=3) is None
    held = a.list_tasks(c)[0]
    assert held['status'] == 'held_budget'
    assert held['attempts'] == 1
    assert a.get_collection(c)['usage']['tokens'] == 6


def test_task_revision_fences_input_and_duplicate_commit(tmp_path):
    a = CollectionStore(tmp_path)
    c = a.create_collection(['source'])['id']
    short = a.add_short(c)
    t = a.enqueue_task(c, 'plan', 'v1', 'key', short_id=short['id'])
    claim = a.claim_task()
    with pytest.raises(ValueError, match='Stale'):
        a.append_revision(short['id'], {}, task_id=t['id'], lease_token=claim['lease_token'], input_version='v0')
    r = a.append_revision(short['id'], {}, task_id=t['id'], lease_token=claim['lease_token'], input_version='v1')
    with pytest.raises(ValueError, match='already committed'):
        a.append_revision(short['id'], {}, r['id'], task_id=t['id'], lease_token=claim['lease_token'], input_version='v1')


def test_duplicate_inputs_and_returned_documents_are_isolated(tmp_path):
    store = CollectionStore(tmp_path)
    with pytest.raises(ValueError, match='Duplicate'):
        store.create_collection(['same', 'same'])
    c = store.create_collection(['source'])['id']
    payload = {'nested': ['original']}
    task = store.enqueue_task(c, 'prepare', 'v1', 'key', payload=payload)
    payload['nested'].append('caller mutation')
    assert task['payload']['nested'] == ['original']
    for changed in ({'stage': 'other'}, {'input_version': 'v2'}, {'reserved_tokens': 1}, {'payload': {}}):
        args = dict(stage='prepare', input_version='v1', idempotency_key='key', payload={'nested': ['original']})
        args.update(changed)
        with pytest.raises(ValueError, match='Idempotency'):
            store.enqueue_task(c, **args)


def test_renewal_and_collection_filter(tmp_path):
    store = CollectionStore(tmp_path)
    a = store.create_collection(['a'])['id']
    b = store.create_collection(['b'])['id']
    store.enqueue_task(a, 'prepare', 'v1', 'key')
    task = store.enqueue_task(b, 'prepare', 'v1', 'key')
    claim = store.claim_task(collection_id=b, now=1, lease_seconds=2)
    assert claim['id'] == task['id']
    assert store.renew_lease(task['id'], claim['lease_token'], 'v1', 10, now=2)['lease_until'] == 12
    assert store.recover_expired(now=4) == []
    with pytest.raises(ValueError, match='Stale'):
        store.renew_lease(task['id'], 'wrong', 'v1', now=4)
    with pytest.raises(ValueError, match='Stale'):
        store.renew_lease(task['id'], claim['lease_token'], 'v1', now=12)


def test_failed_dependency_is_held_and_can_resume(tmp_path):
    store = CollectionStore(tmp_path)
    c = store.create_collection(['source'])['id']
    parent = store.enqueue_task(c, 'prepare', 'v1', 'parent')
    child = store.enqueue_task(c, 'plan', 'v1', 'child', dependencies=[parent['id']])
    claim = store.claim_task(now=1)
    store.finish_task(parent['id'], claim['lease_token'], 'v1', error='failure', now=2)
    assert store.claim_task(now=3) is None
    assert store.list_tasks(c)[1]['status'] == 'held_dependency'
    store.retry_task(parent['id'])
    claim = store.claim_task(now=4)
    store.finish_task(parent['id'], claim['lease_token'], 'v1', now=5)
    store.retry_task(child['id'])
    assert store.claim_task(now=6)['id'] == child['id']


def test_simultaneous_cross_instance_claims_reserve_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    a, b = CollectionStore(tmp_path), CollectionStore(tmp_path)
    c = a.create_collection(['source'], budget={'tokens': 10, 'calls': 1})['id']
    a.enqueue_task(c, 'prepare', 'v1', 'one', reserved_tokens=10, reserved_calls=1)
    a.enqueue_task(c, 'plan', 'v1', 'two', reserved_tokens=10, reserved_calls=1)
    barrier = Barrier(2)

    def claim(store):
        barrier.wait()
        return store.claim_task(now=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, store) for store in (a, b)]
        claims = [f.result() for f in futures]
    assert sum(c is not None for c in claims) == 1
    assert a.get_collection(c)['usage'] == {'tokens': 10, 'calls': 1}
    assert sorted(t['status'] for t in a.list_tasks(c)) == ['active', 'held_budget']


def test_late_cancelled_usage_is_idempotent_without_promotion(tmp_path):
    store = CollectionStore(tmp_path)
    c = store.create_collection(['source'], budget={'tokens': 10, 'calls': 1})['id']
    task = store.enqueue_task(c, 'prepare', 'v1', 'key', reserved_tokens=5, reserved_calls=1)
    claim = store.claim_task(now=1)
    store.cancel_collection(c)
    for _ in range(2):
        attempt = CollectionStore(tmp_path).record_usage(task['id'], claim['lease_token'], actual_tokens=12, actual_calls=1)
        assert attempt['status'] == 'cancelled'
        assert attempt['charged_usage'] == {'tokens': 12, 'calls': 1}
    assert store.get_collection(c)['usage'] == {'tokens': 12, 'calls': 1}
    assert store.list_tasks(c)[0]['status'] == 'cancelled'
    assert store.list_attempts(task['id'])[0]['reported_usage']['tokens'] == 12
    with pytest.raises(ValueError, match='cancelled'):
        store.finish_task(task['id'], claim['lease_token'], 'v1', result={'bad': True}, now=2)


def test_attempt_history_survives_expiry_and_finish_reconciles_once(tmp_path):
    store = CollectionStore(tmp_path)
    c = store.create_collection(['source'], budget={'tokens': 20, 'calls': 2})['id']
    task = store.enqueue_task(c, 'prepare', 'v1', 'key', reserved_tokens=5, reserved_calls=1)
    old = store.claim_task(now=1, lease_seconds=1)
    new = store.claim_task(now=2)
    attempts = store.list_attempts(task['id'])
    assert [a['status'] for a in attempts] == ['expired', 'active']
    assert [a['number'] for a in attempts] == [1, 2]
    assert len({a['id'] for a in attempts}) == 2
    store.record_usage(task['id'], old['lease_token'], actual_tokens=7)
    store.record_usage(task['id'], new['lease_token'], actual_tokens=8)
    store.finish_task(task['id'], new['lease_token'], 'v1', actual_tokens=8, now=3)
    assert store.get_collection(c)['usage'] == {'tokens': 15, 'calls': 2}
    assert [a['status'] for a in store.list_attempts(task['id'])] == ['expired', 'completed']


@pytest.mark.parametrize('bad', [True, -1, 1.0, 0.5, float('inf')])
def test_counts_require_nonnegative_integers(tmp_path, bad):
    store = CollectionStore(tmp_path)
    with pytest.raises(ValueError, match='integer'):
        store.create_collection(['source'], budget={'tokens': bad, 'calls': 1})
    c = store.create_collection(['source'])['id']
    with pytest.raises(ValueError, match='integer'):
        store.enqueue_task(c, 'prepare', 'v1', 'bad', reserved_calls=bad)
    task = store.enqueue_task(c, 'prepare', 'v1', 'good')
    claim = store.claim_task(now=1, lease_seconds=1.5)
    with pytest.raises(ValueError, match='integer'):
        store.record_usage(task['id'], claim['lease_token'], actual_tokens=bad)


def test_resume_grant_adds_one_attempt_to_an_exhausted_failed_task(tmp_path):
    from clippie.collection_store import CollectionStore
    import pytest
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['s1'])
    task = store.enqueue_task(collection['id'], 'discover', 'v1', 'k1', max_attempts=1)
    claimed = store.claim_task(collection_id=collection['id'])
    store.finish_task(claimed['id'], claimed['lease_token'], 'v1', error='provider_service')
    with pytest.raises(ValueError):
        store.retry_task(task['id'])
    retried = store.retry_task(task['id'], grant=1)
    assert retried['status'] == 'queued' and retried['max_attempts'] == 2


def test_extend_run_renews_deadline_and_allowance(tmp_path):
    import time as clock
    from clippie.collection_store import CollectionStore
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['s1'], budget={'tokens': 100, 'calls': 2})
    run = store.start_run(collection['id'], {'deadline': clock.time() - 10}, 'r1')
    extended = store.extend_run(run['id'], tokens=50, calls=3, seconds=600)
    assert extended['status'] == 'active' and extended['deadline'] > clock.time() + 500
    assert store.get_collection(collection['id'])['budget']['calls'] == 5
