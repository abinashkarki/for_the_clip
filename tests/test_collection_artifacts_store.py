import pytest

from clippie.collection_store import CollectionStore


def setup_records(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    short = store.add_short(collection['id'])
    revision = store.append_revision(short['id'], {'text': 'one'})
    task = store.enqueue_task(collection['id'], 'preview', 'v1', 'preview', short_id=short['id'])
    task = store.claim_task(collection_id=collection['id'])
    return store, collection, short, revision, task


def commit(store, short, revision, task, document=None):
    return store.commit_artifact(short['id'], revision['id'], 'preview', document or {'video_path': '/one.mp4'},
                                 task['id'], task['lease_token'], task['input_version'])


def test_artifact_exact_once_and_immutable_links(tmp_path):
    store, collection, short, revision, task = setup_records(tmp_path)
    artifact = commit(store, short, revision, task)
    assert commit(store, short, revision, task, {'video_path': '/other.mp4'}) == artifact
    store.finish_task(task['id'], task['lease_token'], 'v1')
    new_revision = store.append_revision(short['id'], {'text': 'two'}, expected_revision=revision['id'])
    store.enqueue_task(collection['id'], 'preview', 'v2', 'preview2', short_id=short['id'])
    new_task = store.claim_task(collection_id=collection['id'])
    newer = commit(store, short, new_revision, new_task, {'video_path': '/two.mp4'})
    assert newer['id'] != artifact['id']
    assert store.get_artifact(artifact['id'])['document']['video_path'] == '/one.mp4'
    assert len(store.list_artifacts(short['id'])) == 2
    assert store.get_short(short['id'])['current_preview'] == newer['id']


def test_artifact_fences_cancel_stale_and_mixed_revision(tmp_path):
    store, collection, short, revision, task = setup_records(tmp_path)
    other = store.add_short(collection['id'])
    other_revision = store.append_revision(other['id'], {})
    with pytest.raises(ValueError):
        commit(store, short, other_revision, task)
    stale = dict(task, lease_token='obsolete')
    with pytest.raises(ValueError):
        commit(store, short, revision, stale)
    current_revision = store.append_revision(short['id'], {}, expected_revision=revision['id'])
    store.enqueue_task(collection['id'], 'preview', 'v2', 'new-preview', short_id=short['id'])
    current_task = store.claim_task(collection_id=collection['id'])
    current_artifact = commit(store, short, current_revision, current_task)
    old_artifact = commit(store, short, revision, task)
    assert store.get_artifact(old_artifact['id'])['revision_id'] == revision['id']
    assert store.get_short(short['id'])['current_preview'] == current_artifact['id']
    store.cancel_collection(collection['id'])
    with pytest.raises(ValueError):
        commit(store, short, revision, task)
    assert store.list_artifacts(short['id']) == [current_artifact, old_artifact]


def test_decisions_bind_current_revision_and_preserve_history(tmp_path):
    store, collection, short, revision, task = setup_records(tmp_path)
    first = store.save_decision(short['id'], revision['id'], 'reject', 'too long', expected_revision=revision['id'])
    second = store.save_decision(short['id'], revision['id'], 'keep', 'reviewed', expected_revision=revision['id'])
    assert store.get_decision(first['id']) == first
    assert store.list_decisions(short['id'], revision['id']) == [first, second]
    newer = store.append_revision(short['id'], {}, expected_revision=revision['id'])
    with pytest.raises(ValueError):
        store.save_decision(short['id'], revision['id'], 'keep', expected_revision=revision['id'])
    other = store.add_short(collection['id'])
    with pytest.raises(ValueError):
        store.save_decision(other['id'], newer['id'], 'keep', expected_revision=newer['id'])
    assert store.list_decisions(short['id'], newer['id']) == []


def test_invalid_decisions_and_artifact_documents_do_not_persist(tmp_path):
    store, collection, short, revision, task = setup_records(tmp_path)
    for rating, notes in [('accept', ''), ('keep', 'x' * 8001), ('keep', 1)]:
        with pytest.raises(ValueError):
            store.save_decision(short['id'], revision['id'], rating, notes, expected_revision=revision['id'])
    assert store.list_decisions(short['id']) == []
    for document in [{'revision_id': 'wrong'}, {'number': float('nan')}]:
        with pytest.raises(ValueError):
            commit(store, short, revision, task, document)
    assert store.list_artifacts(short['id']) == []


def test_pending_first_preview_promotes_atomically(tmp_path):
    store = CollectionStore(tmp_path)
    collection = store.create_collection(['source'])
    short = store.add_short(collection['id'])
    revision = store.append_revision(short['id'], {'text': 'draft'}, promote=False)
    assert store.get_short(short['id'])['current_revision'] is None
    assert store.get_short(short['id'])['pending_revision'] == revision['id']
    store.enqueue_task(collection['id'], 'preview', 'v1', 'pending', short_id=short['id'])
    task = store.claim_task(collection_id=collection['id'])
    artifact = commit(store, short, revision, task)
    current = store.get_short(short['id'])
    assert current['current_revision'] == revision['id']
    assert current['current_preview'] == artifact['id']
    assert 'pending_revision' not in current


def test_failed_pending_replacement_and_late_preview_preserve_current(tmp_path):
    store, collection, short, revision, task = setup_records(tmp_path)
    original_artifact = commit(store, short, revision, task)
    store.finish_task(task['id'], task['lease_token'], 'v1')
    pending = store.append_revision(short['id'], {'text': 'replacement'}, expected_revision=revision['id'], promote=False)
    store.enqueue_task(collection['id'], 'preview', 'v2', 'failed', short_id=short['id'])
    failed = store.claim_task(collection_id=collection['id'])
    store.finish_task(failed['id'], failed['lease_token'], 'v2', error='render failed')
    current = store.get_short(short['id'])
    assert current['current_revision'] == revision['id']
    assert current['current_preview'] == original_artifact['id']
    newer = store.append_revision(short['id'], {'text': 'manual'}, expected_revision=revision['id'])
    assert 'pending_revision' not in store.get_short(short['id'])
    store.enqueue_task(collection['id'], 'preview', 'v2', 'late', short_id=short['id'])
    late = store.claim_task(collection_id=collection['id'])
    late_artifact = commit(store, short, pending, late)
    assert store.get_artifact(late_artifact['id'])['revision_id'] == pending['id']
    current = store.get_short(short['id'])
    assert current['current_revision'] == newer['id']
    assert current['current_preview'] == original_artifact['id']
