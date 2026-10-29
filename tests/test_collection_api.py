from concurrent.futures import ThreadPoolExecutor
import time

from fastapi.testclient import TestClient
import pytest

from clippie import app as module
from clippie.store import Store


def wait_tasks(client, collection_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get('/api/collections/' + collection_id).json()
        if result['tasks'] and all(task['status'] not in {'queued', 'active'} for task in result['tasks']):
            return result
        time.sleep(.02)
    pytest.fail('Local preparation did not finish.')


@pytest.fixture
def client(tmp_path, monkeypatch):
    storage = Store(tmp_path / 'data')
    monkeypatch.setattr(module, 'STORE', storage)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    media = tmp_path / 'source.mp4'
    media.write_bytes(b'fixture-ready-source')
    storage.save({'id': 'speech', 'status': 'ready', 'source_path': str(media), 'duration': 30,
                  'words': [{'id': 0, 'text': 'Hello', 'start': 0, 'end': 1}]})
    storage.save({'id': 'picture', 'status': 'ready', 'source_path': str(media), 'duration': 30, 'words': []})
    # No lifespan/background work needed: these routes only create/read documents.
    with TestClient(module.app) as result:
        yield result
    module.WORKER.shutdown(wait=True)


def test_workspace_defaults_persist_without_dispatch_or_legacy_mutation(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('Collection workspace creation must not dispatch work.')
    monkeypatch.setattr(module.WORKER, 'submit', unexpected)
    before = module.STORE.get('speech')
    response = client.post('/api/collections', json={'source_ids': ['speech']})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['constraints']['aspect_ratio'] == '9:16'
    assert result['constraints']['max_shorts'] == 12
    assert result['constraints']['form'] == 'auto'
    assert result['capabilities']['generation'] is True
    assert result['shorts'] == result['tasks'] == []
    assert client.get('/api/collections/' + result['id']).json() == result
    assert client.get('/api/collections').json()[0]['id'] == result['id']
    assert module.STORE.get('speech') == before


def test_saved_intake_preferences_do_not_override_explicit_api_constraints(client):
    preferences = client.get('/api/collection-preferences').json()
    saved = client.patch('/api/collection-preferences', json={'expected_revision': preferences['revision'],
        'changes': {'aspect_ratio': '16:9', 'caption_style': 'editorial'}})
    assert saved.status_code == 200
    document = client.post('/api/collections', json={'source_ids': ['speech'],
        'constraints': {'aspect_ratio': '9:16', 'caption_style': 'clean_focus'}})
    assert document.status_code == 201
    assert document.json()['constraints']['aspect_ratio'] == '9:16'
    assert document.json()['constraints']['caption_style'] == 'clean_focus'
    invalid = client.post('/api/collections', json={'source_ids': ['speech'],
        'constraints': {'caption_style': 'unsupported'}})
    assert invalid.status_code == 422


def test_roles_and_intent_validation(client):
    body = {'source_ids': ['speech', 'picture'], 'constraints': {
        'source_roles': {'speech': 'primary', 'picture': 'picture_only'},
        'focus': 'Practical mistakes', 'exclusions': ['Product launches'], 'aspect_ratio': '16:9'}}
    response = client.post('/api/collections', json=body)
    assert response.status_code == 201
    assert response.json()['constraints']['source_roles'] == body['constraints']['source_roles']
    for invalid in [
        {'source_ids': ['picture']},
        {'source_ids': ['speech', 'speech']},
        {'source_ids': ['speech'], 'constraints': {'source_roles': {'speech': 'picture_only'}}},
        {'source_ids': ['speech'], 'constraints': {'source_roles': {'unknown': 'primary'}}},
        {'source_ids': ['speech'], 'constraints': {'max_shorts': 31}},
        {'source_ids': ['speech'], 'constraints': {'max_shorts': True}},
        {'source_ids': ['speech'], 'constraints': {'max_duration': 30, 'preferred_duration': 60}},
        {'source_ids': ['speech'], 'budget': {'calls': -1}},
    ]:
        assert client.post('/api/collections', json=invalid).status_code == 422


def test_cancel_is_a_separate_state_change(client):
    assert client.get('/api/collections').json() == []
    created = client.post('/api/collections', json={'source_ids': ['speech']}).json()
    cancelled = client.post('/api/collections/' + created['id'] + '/cancel')
    assert cancelled.status_code == 200
    assert cancelled.json()['status'] == 'cancelled'
    assert client.get('/api/collections/missing').status_code == 404


def test_prepare_is_durable_idempotent_local_and_reused_across_collections(client):
    from clippie.collection_api import store
    original = module.STORE.get('speech')
    first = client.post('/api/collections', json={'source_ids': ['speech']}).json()['id']
    route = f'/api/collections/{first}/prepare'
    assert client.post(route).status_code == 202
    assert client.post(route).status_code == 202
    result = wait_tasks(client, first)
    assert len(result['tasks']) == 1 and result['tasks'][0]['status'] == 'completed'
    assert result['usage'] == {'tokens': 0, 'calls': 0}
    tasks = store().list_tasks(first)
    assert tasks[0]['attempts'] == 1 and tasks[0]['result']['unit_count'] == 1
    assert 'payload' not in result['tasks'][0] and 'result' not in result['tasks'][0]
    second = client.post('/api/collections', json={'source_ids': ['speech']}).json()['id']
    assert client.post(f'/api/collections/{second}/prepare').status_code == 202
    assert wait_tasks(client, second)['tasks'][0]['status'] == 'completed'
    assert store().list_tasks(second)[0]['result']['cache'] == 'hit'
    assert module.STORE.get('speech') == original
    assert result['capabilities']['generation'] is True
    assert client.post(f'/api/collections/{first}/tasks/missing/retry').status_code == 404
    client.post(f'/api/collections/{first}/cancel')
    assert client.post(route).status_code == 409


def test_changed_input_is_fenced_and_new_version_can_prepare(client, monkeypatch):
    from clippie.collection_api import store
    scheduled = []
    monkeypatch.setattr(module.WORKER, 'submit', lambda function, *args, **kwargs: scheduled.append((function, args, kwargs)))
    collection_id = client.post('/api/collections', json={'source_ids': ['speech']}).json()['id']
    assert client.post(f'/api/collections/{collection_id}/prepare').status_code == 202
    source = module.STORE.get('speech')
    source['words'][0]['text'] = 'Changed.'
    module.STORE.save(source)
    function, args, kwargs = scheduled.pop()
    function(*args, **kwargs)
    old = store().list_tasks(collection_id)[0]
    assert old['status'] == 'failed' and old['result'] is None
    assert client.post(f'/api/collections/{collection_id}/prepare').status_code == 202
    function, args, kwargs = scheduled.pop()
    function(*args, **kwargs)
    tasks = store().list_tasks(collection_id)
    assert len(tasks) == 2 and tasks[1]['status'] == 'completed'
    assert tasks[0]['input_version'] != tasks[1]['input_version']


def test_startup_resumes_existing_queue_after_lease_expiry(tmp_path, monkeypatch):
    from clippie.collection_store import CollectionStore
    from clippie.source_evidence import evidence_key
    source_store = Store(tmp_path / 'data')
    media = tmp_path / 'source.mp4'
    media.write_bytes(b'fixture')
    source = {'id': 'speech', 'status': 'ready', 'duration': 2, 'source_path': str(media),
              'words': [{'id': 0, 'text': 'Hello.', 'start': 0, 'end': 1}]}
    source_store.save(source)
    storage = CollectionStore(source_store.root)
    collection = storage.create_collection(['speech'])
    key = evidence_key(source)
    storage.enqueue_task(collection['id'], 'prepare_source', key, 'once', payload={'source_id': 'speech'})
    abandoned = storage.claim_task(lease_seconds=.2)
    monkeypatch.setattr(module, 'STORE', source_store)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    with TestClient(module.app) as restarted:
        result = wait_tasks(restarted, collection['id'])
        assert result['tasks'][0]['status'] == 'completed'
    module.WORKER.shutdown(wait=True)
    attempts = storage.list_attempts(abandoned['id'])
    assert [attempt['status'] for attempt in attempts] == ['expired', 'completed']


def test_discovery_explanation_is_scoped_to_current_run(client):
    from clippie.collection_api import store
    storage = store()
    collection = client.post('/api/collections', json={'source_ids': ['speech']}).json()
    cid = collection['id']
    old = storage.start_run(cid, {}, 'old')
    task = storage.enqueue_task(cid, 'discover', 'old-discovery', 'old-discovery',
                                payload={'run_id': old['id']})
    claim = storage.claim_task(collection_id=cid)
    storage.finish_task(task['id'], claim['lease_token'], task['input_version'],
                        result={'explanation': 'No supported idea under the earlier focus.'})
    storage.finish_run(old['id'], 'completed')
    assert client.get('/api/collections/' + cid).json()['run']['explanation'].startswith('No supported')
    new = storage.start_run(cid, {}, 'new')
    current = client.get('/api/collections/' + cid).json()
    assert current['run']['id'] == new['id']
    assert 'explanation' not in current['run']
    task = storage.enqueue_task(cid, 'discover', 'new-discovery', 'new-discovery',
                                payload={'run_id': new['id']})
    claim = storage.claim_task(collection_id=cid)
    storage.finish_task(task['id'], claim['lease_token'], task['input_version'],
                        result={'explanation': 'No complete ending in this recording.'})
    storage.finish_run(new['id'], 'completed')
    current = client.get('/api/collections/' + cid).json()
    assert current['run']['explanation'] == 'No complete ending in this recording.'
    assert current['shorts'] == []


def test_warning_decision_requires_explicit_strict_ack_and_public_state_is_revision_bound(client):
    from clippie.collection_api import store
    collection = client.post('/api/collections', json={'source_ids': ['speech']}).json()
    storage = store()
    short = storage.add_short(collection['id'])
    document = {'technical': 'pass', 'editorial': {'status': 'pass', 'issues': [
        {'severity': 'warning', 'category': 'qualification', 'detail': 'Listen to source context'}]}}
    revision = storage.append_revision(short['id'], document)
    route = f'/api/collections/{collection["id"]}/shorts/{short["id"]}/decision'
    body = {'revision_id': revision['id'], 'rating': 'keep'}
    assert client.post(route, json={**body, 'acknowledge_warnings': 'true'}).status_code == 422
    assert client.post(route, json=body).status_code == 200
    details = client.get(f'/api/collections/{collection["id"]}').json()['shorts'][0]
    assert details['export_review_required'] and not details['export_review_acknowledged']
    assert client.post(route, json={**body, 'acknowledge_warnings': True}).status_code == 200
    details = client.get(f'/api/collections/{collection["id"]}').json()['shorts'][0]
    assert details['export_review_acknowledged']
    response = client.post(route, json={**body, 'rating': 'reject', 'acknowledge_warnings': True})
    assert response.status_code == 409 and 'Only a kept' in response.json()['detail']
    assert client.post(route, json={**body, 'rating': 'revise'}).status_code == 200
    assert not client.get(f'/api/collections/{collection["id"]}').json()['shorts'][0]['export_review_acknowledged']
    newer = storage.append_revision(short['id'], document, expected_revision=revision['id'])
    assert client.post(route, json={**body, 'acknowledge_warnings': True}).status_code == 409
    assert not client.get(f'/api/collections/{collection["id"]}').json()['shorts'][0]['export_review_acknowledged']
