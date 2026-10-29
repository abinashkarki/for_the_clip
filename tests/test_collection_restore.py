"""Local restoration fences: no provider, worker, or render calls."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from clippie.collection_store import CollectionStore
from clippie import collection_api


@pytest.fixture
def saved(tmp_path):
    store = CollectionStore(tmp_path)
    cid = store.create_collection(['source'])['id']
    sid = store.add_short(cid)['id']
    doc = {'technical': 'pass', 'editorial': {'status': 'pass'}}
    first = store.append_revision(sid, doc)['id']
    task = store.enqueue_task(cid, 'preview', 'v1', 'preview', short_id=sid)
    claim = store.claim_task()
    preview = store.commit_artifact(sid, first, 'preview', {'profile': 'preview'}, task['id'], claim['lease_token'], 'v1')
    store.finish_task(task['id'], claim['lease_token'], 'v1')
    decision = store.save_decision(sid, first, 'keep', expected_revision=first)
    second = store.append_revision(sid, doc, first)['id']
    return store, cid, sid, first, second, preview, decision


def test_restore_artifacts_decisions_immutable_content_and_budget(saved):
    s, c, short, old, current, preview, decision = saved
    budget = s.get_collection(c)
    revisions = s.list_revisions(short)
    result = s.restore_revision(c, short, old, current, 'restore')
    assert result['current_preview'] == preview['id']
    assert result['current_revision'] == old
    assert result['current_export'] is None
    assert s.list_revisions(short) == revisions
    assert s.list_decisions(short, old) == [decision]
    assert s.list_decisions(short, current) == []
    assert s.get_collection(c) == budget
    assert len(s.list_restorations(c)) == 1


def test_durable_idempotency_does_not_rewind_again(saved):
    s, c, short, old, current, *_ = saved
    original = s.restore_revision(c, short, old, current, 'restore')
    assert original['restoration_id'] == s.list_restorations(c)[0]['id']
    later = s.append_revision(short, {}, old)
    reopened = CollectionStore(s.root)
    assert reopened.restore_revision(c, short, old, current, 'restore') == original
    assert reopened.get_short(short)['current_revision'] == later['id']
    with pytest.raises(ValueError, match='reused'):
        reopened.restore_revision(c, short, old, later['id'], 'restore')
    assert len(s.list_restorations(c)) == 1


@pytest.mark.parametrize('status', ['queued', 'active'])
def test_inflight_tasks_reject_without_mutation(saved, status):
    s, c, short, old, current, *_ = saved
    s.enqueue_task(c, 'plan_short', 'v2', 'replacement', short_id=short)
    if status == 'active':
        s.claim_task()
    before = s.get_short(short)
    with pytest.raises(ValueError, match='queued or active'):
        s.restore_revision(c, short, old, current, 'restore')
    assert s.get_short(short) == before
    assert s.list_restorations(c) == []


@pytest.mark.parametrize('status', ['failed', 'held_budget', 'held_dependency'])
def test_restore_cancels_retry_and_pending_revision(saved, status):
    s, c, short, old, current, *_ = saved
    pending = s.append_revision(short, {}, current, promote=False)
    task = s.enqueue_task(c, 'review_short', 'v2', 'replacement', short_id=short)
    with s._transaction() as db:
        task['status'] = status
        s._save(db, 'task', task)
    result = s.restore_revision(c, short, old, current, 'restore')
    assert 'pending_revision' not in result
    assert s.get_revision(pending['id'])
    assert s.get_task(task['id'])['status'] == 'cancelled'
    with pytest.raises(ValueError, match='retried'):
        s.retry_task(task['id'])


def test_stale_ownership_cancelled_and_missing_preview(saved):
    s, c, short, old, current, *_ = saved
    with pytest.raises(ValueError, match='changed'):
        s.restore_revision(c, short, old, old, 'stale')
    other_c = s.create_collection(['other'])['id']
    other = s.add_short(other_c)['id']
    other_rev = s.append_revision(other, {})['id']
    with pytest.raises(ValueError, match='collection'):
        s.restore_revision(other_c, short, old, current, 'ownership')
    with pytest.raises(ValueError, match='another short'):
        s.restore_revision(c, short, other_rev, current, 'ownership')
    with pytest.raises(ValueError, match='saved preview'):
        s.restore_revision(c, short, current, current, 'preview')
    s.cancel_collection(c)
    with pytest.raises(ValueError, match='cancelled'):
        s.restore_revision(c, short, old, current, 'cancelled')
    assert s.get_short(short)['current_revision'] == current


@pytest.mark.parametrize('document', [{'technical': 'fail', 'editorial': {'status': 'pass'}},
                                       {'technical': 'pass', 'editorial': {'status': 'uncertain'}}, {}])
def test_failed_checks_rejected(saved, document):
    s, c, short, old, current, *_ = saved
    invalid = s.append_revision(short, document, current)['id']
    with pytest.raises(ValueError, match='passing'):
        s.restore_revision(c, short, invalid, invalid, 'bad')


def test_endpoint_uses_saved_artifacts_and_decision(saved, monkeypatch):
    s, c, short, old, current, preview, decision = saved
    monkeypatch.setattr(collection_api, 'store', lambda: s)
    app = FastAPI()
    app.include_router(collection_api.router)
    with TestClient(app) as client:
        url = f'/api/collections/{c}/shorts/{short}/restore'
        body = {'revision_id': old, 'expected_revision': current, 'request_id': 'restore'}
        result = client.post(url, json=body)
        assert result.status_code == 200, result.text
        assert result.json()['preview']['id'] == preview['id']
        assert result.json()['export'] is None
        assert result.json()['decision'] == decision
        assert client.post(url, json={**body, 'request_id': 'stale'}).status_code == 409
        assert client.post(url, json={**body, 'render': True}).status_code == 422


def test_restore_selects_only_target_exports_and_hides_old_pending_review(saved, monkeypatch):
    s, c, short, old, current, *_ = saved
    for revision in (old, current):
        task = s.enqueue_task(c, 'export', revision, 'export-' + revision, short_id=short)
        claim = s.claim_task()
        artifact = s.commit_artifact(short, revision, 'export', {'profile': 'export'}, task['id'], claim['lease_token'], revision)
        s.finish_task(task['id'], claim['lease_token'], revision)
        if revision == old:
            expected = artifact['id']
    task = s.enqueue_task(c, 'review_short', 'v2', 'review', short_id=short)
    claim = s.claim_task()
    s.finish_task(task['id'], claim['lease_token'], 'v2', result={'status': 'uncertain'})
    result = s.restore_revision(c, short, old, current, 'restore')
    monkeypatch.setattr(collection_api, 'store', lambda: s)
    public = collection_api.public_short(result)
    assert public['export']['id'] == expected
    assert public['pending_review'] is None
    assert public['pending_plan'] is None


def test_revision_transcript_uses_own_cache_and_explicit_unavailable(saved, monkeypatch):
    from clippie.source_evidence import prepare_evidence
    s, *_ = saved
    monkeypatch.setattr(collection_api, 'store', lambda: s)
    directory = s.root / 'source-evidence'
    old_source = {'id': 'source', 'duration': 5, 'words': [
        {'id': 0, 'text': 'Old A.', 'start': 0, 'end': 1},
        {'id': 1, 'text': 'Old B.', 'start': 2, 'end': 3}]}
    old = prepare_evidence(old_source, directory)['artifact']
    new = prepare_evidence({**old_source, 'words': [
        {'id': 0, 'text': 'New A.', 'start': 0, 'end': 1}]}, directory)['artifact']
    units = list(reversed(old['units']))
    revision = {'document': {'plan': {
        'unit_ids': [u['id'] for u in units],
        'timeline': {'schema_version': 2, 'clips': [
            {'id': f'c{i}', 'source_id': u['source_id'], 'start': u['start'], 'end': u['end']}
            for i, u in enumerate(units)]},
        'evidence_provenance': {'sources': {'source': old['manifest']['fingerprint']}}}}}
    transcript = collection_api.revision_transcript(revision)
    assert [u['id'] for u in transcript] == [u['id'] for u in units]
    assert [u['text'] for u in transcript] == ['Old B.', 'Old A.']
    assert (directory / f'evidence-{new["manifest"]["fingerprint"]}.json').is_file()
    # A newer cache cannot silently stand in for the revision's missing original evidence.
    (directory / f'evidence-{old["manifest"]["fingerprint"]}.json').unlink()
    unavailable = collection_api.revision_transcript(revision)
    assert [u['id'] for u in unavailable] == [u['id'] for u in units]
    assert all(u['unavailable'] and 'unavailable or invalid' in u['reason'] for u in unavailable)
