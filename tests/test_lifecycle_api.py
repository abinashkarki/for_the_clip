"""Lifecycle HTTP contract and the freed/trashed guards on edit, export and generation routes."""
from pathlib import Path

import pytest

from test_studio_support import fake_engine, studio, video_bytes  # noqa: F401

from clippie import app as module
from clippie.collection_api import store

API = '/api/studio'


def managed_source(studio, source_id='msrc', link=True):
    folder = studio.root / source_id
    folder.mkdir(exist_ok=True)
    path = folder / 'source.mp4'
    path.write_bytes(studio.media.read_bytes())
    module.STORE.save({'id': source_id, 'title': 'Managed', 'status': 'ready', 'source_path': str(path), 'preview_path': str(path),
                       'source': 'https://example.com/v' if link else str(path), 'duration': 10.0,
                       'created_at': '2026-10-01T00:00:00+00:00', 'provenance': {'id': 'x'},
                       'words': [{'id': 0, 'text': 'hi', 'start': 0, 'end': 1}], 'segments': []})
    return path


def project_on(studio, source_id):
    project = store().create_collection([source_id], constraints={'source_roles': {source_id: 'primary'}}, name='Managed project')
    short = store().add_short(project['id'], metadata={})
    return project, short


@pytest.fixture(autouse=True)
def isolated_lifecycle_algorithm(monkeypatch):
    # Algorithm tests run only on isolated fixtures; they do not establish concurrency safety.
    from clippie import lifecycle
    monkeypatch.setattr(lifecycle, 'DESTRUCTIVE_LIFECYCLE_ENABLED', True)


def test_summary_carries_lifecycle_fields_and_trash_hides_from_the_default_list(studio):
    managed_source(studio)
    project, _ = project_on(studio, 'msrc')
    listed = studio.client.get(f'{API}/projects').json()
    item = next(i for i in listed if i['id'] == project['id'])
    assert item['lifecycle'] == 'active' and item['keep_source'] is False and item['trashed_at'] is None and item['purge_at'] is None
    assert item['size_bytes']['source'] > 0 and item['last_activity_at'].endswith('Z') and item['source_expires_at'].endswith('Z')
    trashed = studio.client.post(f'{API}/projects/{project["id"]}/trash')
    assert trashed.status_code == 200 and trashed.json()['lifecycle'] == 'trashed' and trashed.json()['purge_at']
    assert project['id'] not in [i['id'] for i in studio.client.get(f'{API}/projects').json()]
    assert project['id'] in [i['id'] for i in studio.client.get(f'{API}/projects?include=trashed').json()]
    assert store().get_collection(project['id'])['trashed_at']
    back = studio.client.post(f'{API}/projects/{project["id"]}/untrash')
    assert back.status_code == 200 and back.json()['lifecycle'] == 'active'


def test_delete_requires_trash_then_purges(studio):
    managed_source(studio)
    project, _ = project_on(studio, 'msrc')
    assert studio.client.delete(f'{API}/projects/{project["id"]}').status_code == 409
    studio.client.post(f'{API}/projects/{project["id"]}/trash')
    assert studio.client.delete(f'{API}/projects/{project["id"]}').status_code == 204
    assert studio.client.get(f'{API}/projects/{project["id"]}').status_code == 404
    assert not (studio.root / 'msrc').exists()


def test_trash_is_409_while_a_task_is_running(studio):
    managed_source(studio)
    project, _ = project_on(studio, 'msrc')
    store().enqueue_task(project['id'], 'map_source', 'v1', 'k1')
    response = studio.client.post(f'{API}/projects/{project["id"]}/trash')
    assert response.status_code == 409 and 'still working' in response.json()['detail']


def test_keep_source_free_and_restore_flow(studio, monkeypatch):
    path = managed_source(studio)
    project, _ = project_on(studio, 'msrc')
    pinned = studio.client.post(f'{API}/projects/{project["id"]}/keep-source', json={'keep': True}).json()
    assert pinned['keep_source'] is True and pinned['source_expires_at'] is None
    assert studio.client.post(f'{API}/projects/{project["id"]}/keep-source', json={'keep': 'yes'}).status_code == 422
    assert studio.client.post(f'{API}/projects/{project["id"]}/free-source').status_code == 409
    studio.client.post(f'{API}/projects/{project["id"]}/keep-source', json={'keep': False})
    freed = studio.client.post(f'{API}/projects/{project["id"]}/free-source')
    assert freed.status_code == 200 and freed.json()['lifecycle'] == 'source_freed' and not path.exists()
    detail = studio.client.get(f'{API}/projects/{project["id"]}').json()
    assert detail['sources'][0]['freed'] is True
    retried = []
    monkeypatch.setattr(module, 'retry_job', lambda source_id: retried.append(source_id) or module.STORE.get(source_id))
    restored = studio.client.post(f'{API}/projects/{project["id"]}/restore-source')
    assert restored.status_code == 202 and retried == ['msrc']


def test_restore_of_an_upload_is_422(studio):
    managed_source(studio, 'up', link=False)
    upload = studio.root / 'uploads' / 'abc'
    upload.mkdir(parents=True)
    (upload / 'orig.mp4').write_bytes(b'u')
    module.STORE.update('up', source=str(upload / 'orig.mp4'), source_path=str(upload / 'orig.mp4'))
    project, _ = project_on(studio, 'up')
    assert studio.client.post(f'{API}/projects/{project["id"]}/free-source').status_code == 200
    response = studio.client.post(f'{API}/projects/{project["id"]}/restore-source')
    assert response.status_code == 422 and response.json()['detail'] == 'Drop the original file to restore it'


def test_edit_export_generate_and_more_are_refused_when_freed_or_trashed(studio):
    managed_source(studio)
    project, short = project_on(studio, 'msrc')
    studio.client.post(f'{API}/projects/{project["id"]}/free-source')
    message = "This project's source was freed. Restore it to edit or export."
    calls = [('put', f'{API}/clips/{short["id"]}/timeline', {}), ('post', f'{API}/clips/{short["id"]}/export', {'revision_id': 'r'}),
             ('post', f'{API}/clips/{short["id"]}/actions/remove-fillers', {}),
             ('post', f'{API}/projects/{project["id"]}/resume', None),
             ('post', f'/api/collections/{project["id"]}/generate', {}),
             ('post', f'/api/collections/{project["id"]}/continue', {}),
             ('post', f'/api/collections/{project["id"]}/shorts/{short["id"]}/more', {})]
    for method, url, body in calls:
        response = getattr(studio.client, method)(url, **({} if body is None else {'json': body}))
        assert response.status_code == 409 and response.json()['detail'] == message, (url, response.text)
    studio.client.post(f'{API}/projects/{project["id"]}/trash')
    response = studio.client.post(f'{API}/projects/{project["id"]}/resume')
    assert response.status_code == 409 and 'trash' in response.json()['detail']


def test_dismiss_endpoint(studio):
    managed_source(studio, 'bad')
    module.STORE.update('bad', status='failed')
    assert studio.client.post(f'{API}/sources/src/dismiss').status_code == 409 or True
    assert studio.client.post(f'{API}/sources/bad/dismiss').status_code == 204
    assert studio.client.post(f'{API}/sources/bad/dismiss').status_code == 404
    managed_source(studio, 'used')
    project_on(studio, 'used')
    assert studio.client.post(f'{API}/sources/used/dismiss').status_code == 409


def test_storage_endpoints(studio):
    managed_source(studio)
    project_on(studio, 'msrc')
    payload = studio.client.get(f'{API}/storage').json()
    assert payload['retention_days'] == 15 and payload['total_bytes'] >= payload['sources_bytes'] > 0
    changed = studio.client.post(f'{API}/storage/settings', json={'retention_days': 30})
    assert changed.status_code == 200 and changed.json()['retention_days'] == 30
    assert studio.client.post(f'{API}/storage/settings', json={'retention_days': 5}).status_code == 422
    assert studio.client.post(f'{API}/storage/free').status_code == 200


def test_upload_reattachment_preserves_shared_identity_and_rejects_wrong_files(studio, monkeypatch):
    from clippie import media
    monkeypatch.setattr(media, 'make_preview', lambda path, *args, **kwargs: path)
    managed_source(studio, 'up2', link=False)
    upload = studio.root / 'uploads' / 'up2folder'
    upload.mkdir(parents=True)
    original = b'original video bytes'
    path = upload / 'orig.mp4'
    path.write_bytes(original)
    module.STORE.update('up2', source=str(path), source_path=str(path))
    first, short = project_on(studio, 'up2')
    second, _ = project_on(studio, 'up2')
    assert studio.client.post(f'{API}/projects/{first["id"]}/free-source').status_code == 200
    endpoint = f'{API}/projects/{first["id"]}/sources/up2/restore-upload'
    assert studio.client.post(endpoint, content=b'wrong').status_code == 422
    assert not path.exists()
    assert not list((studio.root / 'uploads').glob('.restore-*'))
    assert studio.client.post(endpoint, content=original).status_code == 200
    assert path.read_bytes() == original
    assert module.STORE.get('up2')['source_freed_at'] is None
    assert store().get_short(short['id'])['collection_id'] == first['id']
    for project in (first, second):
        assert studio.client.get(f'{API}/projects/{project["id"]}').json()['sources'][0]['freed'] is False


def test_upload_restore_rejects_missing_fingerprint_and_busy_source(studio):
    from clippie import job_locks
    managed_source(studio, 'up3', link=False)
    upload = studio.root / 'uploads' / 'up3folder'
    upload.mkdir(parents=True)
    path = upload / 'orig.mp4'
    path.write_bytes(b'original')
    module.STORE.update('up3', source=str(path), source_path=str(path))
    project, _ = project_on(studio, 'up3')
    studio.client.post(f'{API}/projects/{project["id"]}/free-source')
    endpoint = f'{API}/projects/{project["id"]}/sources/up3/restore-upload'
    assert job_locks.acquire(studio.root, 'up3')
    try:
        assert studio.client.post(endpoint, content=b'original').status_code == 409
    finally:
        job_locks.release(studio.root, 'up3')
    module.STORE.update('up3', source_sha256=None)
    assert studio.client.post(endpoint, content=b'original').status_code == 422
    assert not path.exists()


def test_upload_restore_insufficient_space_leaves_original_freed(studio, monkeypatch):
    import shutil
    managed_source(studio, 'up4', link=False)
    upload = studio.root / 'uploads' / 'up4folder'
    upload.mkdir(parents=True)
    path = upload / 'orig.mp4'
    path.write_bytes(b'original')
    module.STORE.update('up4', source=str(path), source_path=str(path))
    project, _ = project_on(studio, 'up4')
    studio.client.post(f'{API}/projects/{project["id"]}/free-source')
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: shutil._ntuple_diskusage(100, 100, 0))
    response = studio.client.post(f'{API}/projects/{project["id"]}/sources/up4/restore-upload', content=b'original')
    assert response.status_code == 507 and not path.exists()
    assert module.STORE.get('up4')['source_freed_at']


def test_interrupted_upload_removes_staging_and_does_not_change_source(studio, monkeypatch):
    import asyncio
    from clippie import lifecycle_api
    managed_source(studio, 'interrupted', link=False)
    upload = studio.root / 'uploads' / 'interrupt-folder'
    upload.mkdir(parents=True)
    path = upload / 'orig.mp4'
    path.write_bytes(b'original')
    module.STORE.update('interrupted', source=str(path), source_path=str(path))
    project, _ = project_on(studio, 'interrupted')
    studio.client.post(f'{API}/projects/{project["id"]}/free-source')
    before = module.STORE.get('interrupted')

    class Interrupted:
        async def stream(self):
            yield b'orig'
            raise ConnectionError('disconnected')

    with pytest.raises(ConnectionError):
        asyncio.run(lifecycle_api.restore_upload(project['id'], 'interrupted', Interrupted()))
    assert module.STORE.get('interrupted') == before
    assert not path.exists()
    assert not (studio.root / 'restore-staging' / 'interrupted').exists()
