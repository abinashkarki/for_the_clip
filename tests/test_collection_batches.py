"""Batch intake tests never call a provider or render media."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from clippie import collection_batches as batches
from clippie.collection_store import CollectionStore


class Queue:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def drain(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    storage = CollectionStore(tmp_path)
    sources = {str(i): {'status': 'ready', 'title': 'Source ' + str(i), 'words': [1]} for i in range(4)}
    executor = Queue()
    ctx = SimpleNamespace(STORE=sources, WORKER=executor, KEYS={'gemini': 'fixture'})
    monkeypatch.setattr(batches.api, 'store', lambda: storage)
    monkeypatch.setattr(batches.api, 'context', lambda: ctx)
    monkeypatch.setattr(batches, '_sources', lambda ids: {sid: sources[sid] for sid in ids})
    import clippie.collection_generation as generation
    calls = []

    def start(storage, sources, cid, executor, **kwargs):
        calls.append(cid)
        assert kwargs.pop('dispatch_work') is False
        child = storage.get_collection(cid)
        if child['source_ids'] == ['1']:
            raise ValueError('private provider error must not escape')
        return storage.start_run(cid, {'deadline': 100, **kwargs}, kwargs['request_id'])

    monkeypatch.setattr(generation, 'start_generation', start)
    monkeypatch.setattr(generation, 'drain_generation', lambda *args: None)
    return storage, sources, executor, calls, ctx


def test_atomic_independent_intake_overrides_and_idempotency(setup):
    storage, sources, executor, calls, ctx = setup
    request = batches.BatchRequest(request_id='first', source_ids=list(sources), constraints={'focus': 'shared'},
                                   overrides={'2': {'focus': 'specific'}}, calls_per_source=3)
    batch = batches.create_batch(request)
    assert batch['initial_allocated']['calls'] == 12
    assert batch['usage']['calls'] == 0
    assert batches.create_batch(request)['id'] == batch['id']
    assert len(storage.list_collections()) == 4
    for member in batch['members']:
        child = storage.get_collection(member['collection_id'])
        sid = member['source_id']
        assert child['source_ids'] == [sid]
        assert child['constraints']['source_roles'] == {sid: 'primary'}
        assert child['constraints']['focus'] == ('specific' if sid == '2' else 'shared')
    with pytest.raises(HTTPException) as error:
        batches.create_batch(request.model_copy(update={'calls_per_source': 4}))
    assert error.value.status_code == 409
    assert not calls


def test_start_failure_restart_and_deadline_waits_for_worker(setup):
    storage, sources, executor, calls, ctx = setup
    batch = batches.create_batch(batches.BatchRequest(request_id='run', source_ids=list(sources)))
    result = batches.start_batch(batch['id'])
    assert len(executor.jobs) == 4
    assert all(m['status'] == 'queued' for m in result['members'])
    assert all(not c.get('current_run') for c in storage.list_collections())
    batches.resume_batches(storage.root, sources, executor)
    assert len(executor.jobs) == 4
    executor.drain()
    result = batches.get_batch(batch['id'])
    assert len(calls) == 4
    assert result['members'][1]['error'] == 'start_failed'
    assert sum(bool(c.get('current_run')) for c in storage.list_collections()) == 3
    batches.resume_batches(storage.root, sources, executor)
    executor.drain()
    assert len(calls) == 5  # Only the failed, never-started child may retry.
    assert result['current_allocated'] == result['initial_allocated']


def test_cancel_fences_queued_children_and_preserves_terminal(setup):
    storage, sources, executor, calls, ctx = setup
    batch = batches.create_batch(batches.BatchRequest(request_id='cancel', source_ids=list(sources)))
    first = batch['members'][0]['collection_id']
    run = storage.start_run(first, {}, 'existing')
    storage.finish_run(run['id'], 'completed')
    batches.start_batch(batch['id'])
    batches.cancel_batch(batch['id'])
    executor.drain()
    assert not calls
    assert storage.get_collection(first)['status'] != 'cancelled'
    assert all(storage.get_collection(m['collection_id'])['status'] == 'cancelled' for m in batch['members'][1:])
    batches.resume_batches(storage.root, sources, executor)
    assert not executor.jobs


@pytest.mark.parametrize('changes', [
    {'source_ids': ['0', '0']}, {'source_ids': list('01234')},
    {'overrides': {'9': {'focus': 'bad'}}}, {'overrides': {'0': {'unknown': True}}},
    {'overrides': {'0': {'source_roles': {'1': 'primary'}}}},
    {'constraints': {'source_roles': {'0': 'picture_only'}}}, {'calls_per_source': True},
    {'overrides': {'0': {'max_duration': 10, 'preferred_duration': 20}}},
])
def test_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        batches.BatchRequest.model_validate({'request_id': 'validate', 'source_ids': ['0'], **changes})


@pytest.mark.parametrize('source', [
    {'status': 'failed', 'words': [1]}, {'status': 'ready', 'words': []},
])
def test_every_source_requires_ready_speech(monkeypatch, source):
    from clippie import app as production
    monkeypatch.setattr(production, 'sources_for', lambda ids, **kwargs: {'source': source})
    with pytest.raises(HTTPException) as error:
        batches._sources(['source'])
    assert error.value.status_code == 422


def test_no_key_does_not_authorize_or_schedule(setup):
    storage, sources, executor, calls, ctx = setup
    batch = batches.create_batch(batches.BatchRequest(request_id='no-key', source_ids=['0']))
    ctx.KEYS.clear()
    with pytest.raises(HTTPException):
        batches.start_batch(batch['id'])
    assert batches.get_batch(batch['id'])['status'] == 'draft'
    batches.resume_batches(storage.root, sources, executor)
    assert not executor.jobs
