from concurrent.futures import ThreadPoolExecutor
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from clippie import collection_preferences as prefs
from clippie.collection_store import CollectionStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    storage = CollectionStore(tmp_path)
    monkeypatch.setattr(prefs.collection_api, 'store', lambda: storage)
    app = FastAPI()
    app.include_router(prefs.router)
    return storage, TestClient(app)


def test_query_patch_reopen_reset_scoped(setup):
    storage, client = setup
    collection = storage.create_collection(['source'])
    with storage._transaction() as db:
        before = db.execute('SELECT * FROM records').fetchall()
    assert client.get('/api/collection-preferences').json() == dict(revision=0, overrides={}, effective=prefs.DEFAULTS, defaults=prefs.DEFAULTS)
    result = client.patch('/api/collection-preferences', json={'expected_revision': 0, 'changes': {'captions': False, 'form': 'story'}})
    assert result.status_code == 200
    assert prefs.load(CollectionStore(storage.root))['overrides'] == {'captions': False, 'form': 'story'}
    result = client.patch('/api/collection-preferences', json={'expected_revision': 1, 'reset_fields': ['form']}).json()
    assert result['revision'] == 2
    assert result['overrides'] == {'captions': False}
    assert result['effective']['form'] == 'auto'
    assert storage.get_collection(collection['id']) == collection
    with storage._transaction() as db:
        assert db.execute('SELECT * FROM records WHERE kind != ?', (prefs.KIND,)).fetchall() == before


def test_stale_and_noop(setup):
    storage, client = setup
    patch = {'expected_revision': 0, 'changes': {'max_shorts': 2}}
    assert client.patch('/api/collection-preferences', json=patch).json()['revision'] == 1
    assert client.patch('/api/collection-preferences', json=patch).status_code == 409
    patch['expected_revision'] = 1
    assert client.patch('/api/collection-preferences', json=patch).json()['revision'] == 1
    assert prefs.load(storage)['effective']['max_shorts'] == 2


def test_concurrent_cas(setup):
    storage, _ = setup
    def write(value):
        try:
            return prefs.update(CollectionStore(storage.root), {'expected_revision': 0, 'changes': {'max_shorts': value}})['revision']
        except prefs.RevisionConflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(write, [1, 2]), key=str) == [1, 'conflict']
    assert prefs.load(storage)['revision'] == 1


@pytest.mark.parametrize('body', [
    {'expected_revision': True}, {'expected_revision': -1}, {'expected_revision': '0'},
    {'expected_revision': 0, 'changes': None}, {'expected_revision': 0, 'reset_fields': None},
    *[{'expected_revision': 0, 'changes': {key: value}} for key, value in [
        ('api_key', 'secret'), ('source_ids', ['source']), ('focus', 'secret'), ('audience', 'secret'),
        ('max_shorts', True), ('max_shorts', 31), ('captions', 1), ('form', None), ('form', 'other'),
        ('max_duration', True), ('max_duration', '20'), ('max_duration', 0), ('max_duration', 181),
        ('request_calls', True), ('request_calls', 9), ('aspect_ratio', '1:1'), ('caption_style', 'other')]],
    {'expected_revision': 0, 'changes': {'form': 'story'}, 'reset_fields': ['form']},
    {'expected_revision': 0, 'reset_fields': ['api_key']},
    {'expected_revision': 0, 'reset_fields': ['form'] * 10},
    {'expected_revision': 0, 'api_key': 'secret'},
])
def test_invalid_requests_preserve_empty_store(setup, body):
    storage, client = setup
    assert client.patch('/api/collection-preferences', json=body).status_code == 422
    assert prefs.load(storage)['revision'] == 0
    with storage._transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM records').fetchone()[0] == 0


@pytest.mark.parametrize('value', [float('inf'), float('nan'), -float('inf')])
def test_nonfinite_rejected(value):
    with pytest.raises(ValidationError):
        prefs.PreferencePatch(expected_revision=0, changes={'max_duration': value})


@pytest.mark.parametrize('document', ['broken', json.dumps({'revision': 2, 'overrides': {}})])
def test_corrupt_or_old_schema_preserved(setup, document):
    storage, client = setup
    with storage._transaction() as db:
        db.execute('INSERT INTO records VALUES (?,?,?,?)', (prefs.IDENTITY, prefs.KIND, None, document))
    assert client.get('/api/collection-preferences').status_code == 500
    assert client.patch('/api/collection-preferences', json={'expected_revision': 0}).status_code == 500
    with storage._transaction() as db:
        assert db.execute('SELECT document FROM records').fetchone()[0] == document


def test_phrase_caption_preference_persists_without_changing_default(setup):
    storage, client = setup
    result = client.patch('/api/collection-preferences', json={
        'expected_revision': 0, 'changes': {'caption_style': 'phrase'}})
    assert result.status_code == 200
    assert result.json()['effective']['caption_style'] == 'phrase'
    assert prefs.load(CollectionStore(storage.root))['effective']['caption_style'] == 'phrase'
    assert result.json()['defaults']['caption_style'] == 'auto'
    reset = client.patch('/api/collection-preferences', json={
        'expected_revision': 1, 'reset_fields': ['caption_style']})
    assert reset.json()['effective']['caption_style'] == 'auto'


def test_canvas_preference_persists_and_scoped_reset_defaults_to_plain(setup):
    storage, client = setup
    result = client.patch('/api/collection-preferences', json={
        'expected_revision': 0, 'changes': {'canvas_style': 'blurred', 'caption_style': 'phrase'}})
    assert result.status_code == 200
    assert prefs.load(CollectionStore(storage.root))['effective']['canvas_style'] == 'blurred'
    result = client.patch('/api/collection-preferences', json={
        'expected_revision': 1, 'reset_fields': ['canvas_style']})
    assert result.json()['effective']['canvas_style'] == 'plain'
    assert result.json()['effective']['caption_style'] == 'phrase'
