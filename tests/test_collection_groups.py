from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from clippie import app as module
from clippie.collection_store import CollectionStore, CollectionRequestConflict
from clippie.store import Store


def test_creation_request_is_durable_atomic_and_snapshotted(tmp_path):
    store = CollectionStore(tmp_path)
    constraints = {'source_roles': {'a': 'primary', 'b': 'primary'}}
    def create(_):
        return CollectionStore(tmp_path).create_collection(['a', 'b'], constraints, name=' Group ', request_id='key')
    with ThreadPoolExecutor(max_workers=4) as pool:
        documents = list(pool.map(create, range(8)))
    assert len({doc['id'] for doc in documents}) == 1
    document = documents[0]
    constraints['source_roles']['b'] = 'picture_only'
    assert store.get_collection(document['id'])['constraints']['source_roles']['b'] == 'primary'
    store.extend_budget(document['id'], calls=2, request_id='extend')
    store.cancel_collection(document['id'])
    replay = CollectionStore(tmp_path).create_collection(
        ['a', 'b'], {'source_roles': {'a': 'primary', 'b': 'primary'}}, name='Group', request_id='key')
    assert replay['status'] == 'cancelled'
    assert replay['budget']['calls'] == 2
    assert store.list_tasks(document['id']) == []
    assert 'current_run' not in replay
    for changed in ({'name': 'Other'}, {'source_ids': ['b', 'a']}, {'budget': {'calls': 1}}, {'constraints': {}}):
        inputs = dict(source_ids=['a', 'b'], constraints={'source_roles': {'a': 'primary', 'b': 'primary'}}, name='Group', request_id='key')
        inputs.update(changed)
        with pytest.raises(CollectionRequestConflict):
            store.create_collection(**inputs)
    assert store.create_collection(['a'])['id'] != store.create_collection(['a'])['id']


@pytest.fixture
def client(tmp_path, monkeypatch):
    storage = Store(tmp_path / 'data')
    monkeypatch.setattr(module, 'STORE', storage)
    media = tmp_path / 'source.mp4'
    media.write_bytes(b'fixture')
    for identity in ['a', 'b', 'picture']:
        storage.save({'id': identity, 'status': 'ready', 'source_path': str(media), 'duration': 30,
                      'words': [] if identity == 'picture' else [{'id': 0, 'text': 'Hello', 'start': 0, 'end': 1}]})
    with TestClient(module.app) as result:
        yield result


def test_api_replay_before_source_validation_preserves_status(client, monkeypatch):
    from clippie import app as production_api, collection_api
    body = {'source_ids': ['a', 'b', 'picture'], 'name': '  Interview  ', 'request_id': 'create-1',
            'constraints': {'source_roles': {'a': 'primary', 'b': 'primary', 'picture': 'picture_only'}}}
    response = client.post('/api/collections', json=body)
    assert response.status_code == 201, response.text
    document = response.json()
    assert document['name'] == document['title'] == 'Interview'
    collection_api.store().cancel_collection(document['id'])
    def missing(*args, **kwargs):
        pytest.fail('Replay must not inspect sources or refresh source stats')
    monkeypatch.setattr(production_api, 'sources_for', missing)
    monkeypatch.setattr(module.STORE, 'get', lambda *_: (_ for _ in ()).throw(KeyError('missing')))
    replay = client.post('/api/collections', json=body)
    assert replay.status_code == 201
    assert replay.json()['id'] == document['id']
    assert replay.json()['status'] == 'cancelled'
    assert replay.json()['tasks'] == []
    assert 'current_run' not in replay.json()
    changed = client.post('/api/collections', json={**body, 'name': 'Changed'})
    assert changed.status_code == 409


@pytest.mark.parametrize('metadata', [{'name': 2}, {'name': 'a\nb'}, {'name': 'x' * 101},
                                    {'request_id': ''}, {'request_id': ' '}, {'request_id': 3}])
def test_invalid_metadata(client, metadata):
    assert client.post('/api/collections', json={'source_ids': ['a'], **metadata}).status_code == 422


def test_picture_only_cannot_supply_narration_or_expand_role_scope(client):
    for body in [
        {'source_ids': ['picture']},
        {'source_ids': ['a', 'picture'], 'constraints': {'source_roles': {'a': 'picture_only'}}},
        {'source_ids': ['a'], 'constraints': {'source_roles': {'b': 'primary'}}},
        {'source_ids': ['a'], 'constraints': {'source_roles': {'a': 'narrator'}}},
    ]:
        assert client.post('/api/collections', json=body).status_code == 422
