from concurrent.futures import ThreadPoolExecutor

import pytest

from clippie.collection_store import CollectionStore


def active(store, collection, key='slate', version='v1'):
    store.enqueue_task(collection['id'], key, version, key)
    return store.claim_task(collection_id=collection['id'])


def test_superseded_run_cannot_commit_or_consume_another_reservation(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'], budget={'tokens': 0, 'calls': 2})
    first = store.start_run(collection['id'], {}, 'first')
    old = store.enqueue_task(collection['id'], 'map', 'v1', 'old', reserved_calls=1,
                             payload={'run_id': first['id']})
    lease = store.claim_task(now=10, lease_seconds=2)
    store.finish_run(first['id'], 'needs_attention')
    second = store.start_run(collection['id'], {}, 'second')
    with pytest.raises(ValueError, match='Stale collection run'):
        store.finish_task(old['id'], lease['lease_token'], 'v1', now=11)
    new = store.enqueue_task(collection['id'], 'map', 'v2', 'new', reserved_calls=1,
                             payload={'run_id': second['id']})
    assert store.claim_task(now=13)['id'] == new['id']
    assert store.get_task(old['id'])['status'] == 'cancelled'
    assert store.get_collection(collection['id'])['usage']['calls'] == 2


def commit(store, collection, task, candidates=None):
    return store.save_candidates(collection['id'], candidates or [{'id': 'candidate', 'disposition': 'keep', 'reasons': ['test']}],
                                 task['input_version'], task['id'], task['lease_token'])


def test_candidate_commit_and_short_adoption(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    task = active(store, collection)
    original = commit(store, collection, task)
    assert commit(store, collection, task, [{'id': 'different'}]) == original
    kwargs = dict(task_id=task['id'], lease_token=task['lease_token'], input_version='v1')
    short = store.ensure_short(collection['id'], 'candidate', **kwargs)
    assert store.ensure_short(collection['id'], 'candidate', **kwargs) == short
    assert store.get_candidate(collection['id'], 'candidate')['document']['reasons'] == ['test']
    assert store.get_task(task['id'])['committed_candidates'] == [original[0]['id']]
    store.finish_task(task['id'], task['lease_token'], 'v1', error='interrupted')
    store.retry_task(task['id'])
    retry = store.claim_task(collection_id=collection['id'])
    assert commit(store, collection, retry) == original
    assert store.ensure_short(collection['id'], 'candidate', task_id=retry['id'], lease_token=retry['lease_token'], input_version='v1') == short
    assert len(store.list_shorts(collection['id'])) == 1


def test_cancel_and_cross_collection_fences(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    other = store.create_collection(['other'])
    task = active(store, collection)
    with pytest.raises(ValueError):
        commit(store, other, task)
    commit(store, collection, task)
    with pytest.raises(ValueError):
        store.ensure_short(other['id'], 'candidate', task_id=task['id'], lease_token=task['lease_token'], input_version='v1')
    store.cancel_collection(collection['id'])
    with pytest.raises(ValueError):
        commit(store, collection, task)
    assert store.extend_budget(collection['id'], 10, 1, request_id='after-cancel')['status'] == 'cancelled'


def test_budget_extension_concurrent_idempotency_and_funding(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    task = store.enqueue_task(collection['id'], 'slate', 'v1', 'slate', reserved_tokens=10, reserved_calls=1)
    assert store.claim_task(collection_id=collection['id']) is None
    store.extend_budget(collection['id'], 5, 0, request_id='partial')
    assert store.get_task(task['id'])['status'] == 'held_budget'
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: store.extend_budget(collection['id'], 5, 1, request_id='same'), range(8)))
    assert store.get_collection(collection['id'])['budget'] == {'tokens': 10, 'calls': 1}
    assert store.get_task(task['id'])['status'] == 'queued'
    assert store.claim_task(collection_id=collection['id'])['id'] == task['id']
    with pytest.raises(ValueError):
        store.extend_budget(collection['id'], -1, 0, request_id='bad')
    with pytest.raises(ValueError):
        store.extend_budget(collection['id'], 1, 1, request_id='same')


def test_candidate_versions_are_immutable(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    first = active(store, collection)
    commit(store, collection, first)
    store.finish_task(first['id'], first['lease_token'], 'v1')
    second = active(store, collection, key='second', version='v2')
    commit(store, collection, second, [{'id': 'candidate', 'disposition': 'reject', 'reasons': ['new evidence']}])
    assert store.get_candidate(collection['id'], 'candidate', 'v1')['document']['disposition'] == 'keep'
    assert store.get_candidate(collection['id'], 'candidate', 'v2')['document']['disposition'] == 'reject'


def test_runs_snapshot_idempotency_exclusion_and_cancellation(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    configuration = {'provider': 'test', 'source_versions': {'source': 'v1'}, 'constraints': {}, 'deadline': 123, 'max_output_tokens': 100}
    run = store.start_run(collection['id'], configuration, 'request')
    configuration['source_versions']['source'] = 'mutated'
    assert store.get_run(run['id'])['configuration']['source_versions']['source'] == 'v1'
    assert store.start_run(collection['id'], run['configuration'], 'request') == run
    with pytest.raises(ValueError):
        store.start_run(collection['id'], configuration, 'request')
    with pytest.raises(ValueError):
        store.start_run(collection['id'], configuration, 'second')
    store.finish_run(run['id'], 'completed')
    second = store.start_run(collection['id'], configuration, 'second')
    with pytest.raises(ValueError):
        store.finish_run(run['id'], 'completed')
    store.cancel_collection(collection['id'])
    assert store.get_run(second['id'])['status'] == 'cancelled'
    with pytest.raises(ValueError):
        store.finish_run(second['id'], 'completed')


def test_continue_run_atomic_idempotent_and_immutable_config(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    config = {'source_versions': {'source': 'v1'}, 'deadline': 1}
    run = store.start_run(collection['id'], config, 'start')
    task = store.enqueue_task(collection['id'], 'provider', 'v1', 'provider', reserved_tokens=10, reserved_calls=1, payload={'run_id': run['id']})
    assert store.claim_task(collection_id=collection['id']) is None
    store.finish_run(run['id'], 'needs_attention')
    continued = store.continue_run(run['id'], 10, 1, 60, 'continue')
    assert continued['configuration'] == config
    assert continued['deadline'] > config['deadline']
    assert continued['status'] == 'active'
    assert store.get_task(task['id'])['status'] == 'queued'
    assert store.continue_run(run['id'], 10, 1, 60, 'continue') == continued
    assert store.get_collection(collection['id'])['budget'] == {'tokens': 10, 'calls': 1}
    with pytest.raises(ValueError):
        store.continue_run(run['id'], 20, 1, 60, 'continue')
    assert store.get_collection(collection['id'])['budget'] == {'tokens': 10, 'calls': 1}
    store.cancel_collection(collection['id'])
    with pytest.raises(ValueError):
        store.continue_run(run['id'], 10, 1, 60, 'continue')


def test_continue_run_rejects_empty_and_invalid_allowances(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    run = store.start_run(collection['id'], {}, 'start')
    with pytest.raises(ValueError):
        store.continue_run(run['id'], 10, 1, 60, 'empty')
    for tokens, calls, seconds in [(True, 1, 60), (1, -1, 60), (1, 1, 29), (1, 1, 901)]:
        with pytest.raises(ValueError):
            store.continue_run(run['id'], tokens, calls, seconds, 'invalid')
    assert store.get_collection(collection['id'])['budget'] == {'tokens': 0, 'calls': 0}
