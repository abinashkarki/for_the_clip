"""Usage diagnostics use persisted attempts without provider work or charge changes."""
from types import SimpleNamespace

import pytest

from clippie import collection_api as api, collection_batches as batches
from clippie.collection_store import CollectionStore


@pytest.fixture
def storage(tmp_path):
    return CollectionStore(tmp_path)


def collection(storage):
    return storage.create_collection(['source'], budget={'calls': 20, 'tokens': 1000})['id']


def claim(storage, cid, key='paid', **kwargs):
    storage.enqueue_task(cid, 'plan', 'v1', key, reserved_calls=1, reserved_tokens=10, **kwargs)
    return storage.claim_task(collection_id=cid, now=1)


def finish(storage, task, **kwargs):
    storage.finish_task(task['id'], task['lease_token'], 'v1', now=2, **kwargs)


def test_known_partial_and_zero(storage):
    cid = collection(storage)
    first = claim(storage, cid, 'first')
    finish(storage, first, actual_calls=1, actual_tokens=25)
    second = claim(storage, cid, 'second')
    finish(storage, second, actual_calls=0)
    third = claim(storage, cid, 'third')
    storage.record_usage(third['id'], third['lease_token'], actual_tokens=0)
    assert storage.usage_report(cid) == {
        'attempts': 3, 'reported_calls': 1, 'reported_tokens': 25,
        'unknown_call_attempts': 1, 'unknown_token_attempts': 1, 'active_attempts': 1,
        'cost': {'status': 'unknown', 'amount': None, 'currency': None}}


def test_cancelled_late_usage_and_reopen(storage):
    cid = collection(storage)
    task = claim(storage, cid)
    storage.cancel_collection(cid)
    assert storage.usage_report(cid)['unknown_token_attempts'] == 1
    storage.record_usage(task['id'], task['lease_token'], actual_calls=1, actual_tokens=42)
    report = CollectionStore(storage.root).usage_report(cid)
    assert report['attempts'] == 1
    assert report['reported_tokens'] == 42
    assert report['unknown_token_attempts'] == report['active_attempts'] == 0


def test_retries_and_superseded_runs(storage):
    cid = collection(storage)
    run = storage.start_run(cid, {}, 'old')
    task = claim(storage, cid, payload={'run_id': run['id']})
    finish(storage, task, error='transient', actual_calls=1, actual_tokens=2)
    storage.retry_task(task['id'])
    retry = storage.claim_task(collection_id=cid, now=3)
    storage.finish_task(retry['id'], retry['lease_token'], 'v1', now=4, actual_calls=1)
    storage.finish_run(run['id'], 'completed')
    storage.start_run(cid, {}, 'new')
    report = storage.usage_report(cid)
    assert report['attempts'] == report['reported_calls'] == 2
    assert report['unknown_token_attempts'] == 1


def test_local_queued_isolation_and_readonly(storage):
    cid, other = collection(storage), collection(storage)
    storage.enqueue_task(cid, 'local', 'v1', 'local')
    local = storage.claim_task(collection_id=cid, now=1)
    finish(storage, local)
    storage.enqueue_task(cid, 'paid', 'v1', 'queued', reserved_calls=1)
    claim(storage, other)
    before = storage.path.read_bytes()
    assert storage.usage_report(cid)['attempts'] == 0
    assert storage.usage_report(other)['attempts'] == 1
    assert storage.path.read_bytes() == before
    with pytest.raises(KeyError):
        storage.usage_report('missing')


def test_public_detail_and_batch_additivity(storage, monkeypatch):
    ids = [collection(storage), collection(storage)]
    task = claim(storage, ids[0])
    finish(storage, task, actual_calls=1, actual_tokens=0)
    claim(storage, ids[1])
    monkeypatch.setattr(api, 'store', lambda: storage)
    monkeypatch.setattr(api, 'context', lambda: SimpleNamespace(
        STORE={'source': {'title': 'Source'}}, KEYS={}))
    detail = api.get_collection(ids[0])
    assert detail['usage_report'] == storage.usage_report(ids[0])
    assert 'usage_report' not in api.public(storage.get_collection(ids[0]))
    batch = dict(id='batch', status='started', initial_allocated={'calls': 0, 'tokens': 0},
                 members=[{'collection_id': cid} for cid in ids + [ids[0]]])
    result = batches.public_batch(storage, batch)
    assert result['usage_report']['attempts'] == 2
    assert result['usage_report']['reported_calls'] == 1
    assert result['usage_report']['unknown_token_attempts'] == 1
    assert result['usage_report']['active_attempts'] == 1
    assert result['members'][0]['usage_report'] == detail['usage_report']
    assert result['usage_report']['cost']['status'] == 'unknown'
