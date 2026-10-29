"""Revision-specific warning disposition gates enqueue, render and atomic publication."""
from types import SimpleNamespace
import time

import pytest
from pydantic import ValidationError

from clippie.collection_api import DecisionRequest
from clippie.collection_store import CollectionStore
from clippie import collection_generation, collection_media, collection_workflow


def records(tmp_path, *, issues=None, technical='pass', editorial='pass'):
    storage = CollectionStore(tmp_path)
    collection = storage.create_collection(['source'])
    short = storage.add_short(collection['id'])
    document = {'technical': technical, 'editorial': {'status': editorial, 'issues': issues or []}}
    revision = storage.append_revision(short['id'], document)
    return storage, collection, short, revision


def warning():
    return [{'category': 'meaning', 'severity': 'warning', 'detail': 'Review qualification'}]


def decide(storage, short, revision, rating='keep', acknowledge=True):
    return storage.save_decision(short['id'], revision['id'], rating,
                                 expected_revision=revision['id'], acknowledge_warnings=acknowledge)


def export_task(storage, collection, short, key='export'):
    storage.enqueue_task(collection['id'], 'render_export', 'v1', key, short_id=short['id'])
    return storage.claim_task(collection_id=collection['id'])


def commit(storage, short, revision, task):
    return storage.commit_artifact(short['id'], revision['id'], 'export', {'video_path': '/qa.mp4'},
                                   task['id'], task['lease_token'], task['input_version'])


def test_warning_ack_is_explicit_latest_and_revision_bound(tmp_path):
    storage, collection, short, revision = records(tmp_path, issues=warning())
    assert storage.export_review_state(revision['id']) == {
        'export_review_required': True, 'export_review_acknowledged': False, 'export_eligible': False}
    decide(storage, short, revision, acknowledge=False)
    with pytest.raises(ValueError, match='acknowledgment'):
        storage.export_review_state(revision['id'], require=True)
    saved = decide(storage, short, revision)
    assert saved['acknowledge_warnings'] is True
    assert storage.export_review_state(revision['id'], require=True)['export_review_acknowledged']
    newer = storage.append_revision(short['id'], revision['document'], expected_revision=revision['id'])
    assert storage.export_review_state(revision['id'], require=True)['export_eligible']
    assert not storage.export_review_state(newer['id'])['export_eligible']
    with pytest.raises(ValueError, match='no longer current'):
        decide(storage, short, revision)


@pytest.mark.parametrize('rating', ['revise', 'reject', 'keep'])
def test_later_decision_revokes_and_commit_checks_inside_transaction(tmp_path, rating):
    storage, collection, short, revision = records(tmp_path, issues=warning())
    decide(storage, short, revision)
    task = export_task(storage, collection, short)
    storage.export_review_state(revision['id'], require=True)  # Eligible when render starts.
    decide(storage, short, revision, rating=rating, acknowledge=False)  # Changes during rendering.
    with pytest.raises(ValueError, match='acknowledgment'):
        commit(storage, short, revision, task)
    assert storage.list_artifacts(short['id']) == []
    assert 'committed_artifact' not in storage.get_task(task['id'])
    decide(storage, short, revision)
    artifact = commit(storage, short, revision, task)
    decide(storage, short, revision, rating='reject', acknowledge=False)
    assert commit(storage, short, revision, task) == artifact  # Existing artifact remains immutable.
    assert storage.get_artifact(artifact['id']) == artifact


@pytest.mark.parametrize('technical,editorial,issues', [
    ('fail', 'pass', warning()), ('pass', 'uncertain', warning()),
    ('pass', 'needs_revision', warning()),
    ('pass', 'pass', [{'severity': 'critical', 'category': 'source_integrity'}]),
    ('pass', 'pass', []),
])
def test_ack_cannot_override_machine_checks_or_ack_nonexistent_warnings(tmp_path, technical, editorial, issues):
    storage, _, short, revision = records(tmp_path, technical=technical, editorial=editorial, issues=issues)
    with pytest.raises(ValueError, match='Only a kept'):
        decide(storage, short, revision)
    assert storage.list_decisions(short['id']) == []
    if technical != 'pass' or editorial != 'pass' or any(i['severity'] == 'critical' for i in issues):
        assert not storage.export_review_state(revision['id'])['export_eligible']


def test_clean_export_needs_no_keep(tmp_path):
    storage, collection, short, revision = records(tmp_path)
    assert storage.export_review_state(revision['id'], require=True)['export_eligible']
    assert commit(storage, short, revision, export_task(storage, collection, short))['revision_id'] == revision['id']


def test_enqueue_uses_exact_warning_decisions_and_clean_exports_remain_allowed(tmp_path, monkeypatch):
    storage, collection, short, revision = records(tmp_path, issues=warning())
    storage.start_run(collection['id'], {'deadline': time.time() + 60}, 'warning-export')
    monkeypatch.setattr(collection_workflow, 'dispatch', lambda *args: None)
    selected = [{'short_id': short['id'], 'revision_id': revision['id']}]
    with pytest.raises(ValueError, match='acknowledgment'):
        collection_generation.export_revisions(storage, {}, collection['id'], selected, None)
    assert storage.list_tasks(collection['id']) == []
    decide(storage, short, revision)
    collection_generation.export_revisions(storage, {}, collection['id'], selected, None)
    assert storage.list_tasks(collection['id'])[0]['stage'] == 'render_export'


def test_render_export_rechecks_before_creating_media(tmp_path):
    _, _, short, revision = records(tmp_path / 'store', issues=warning())
    ctx = SimpleNamespace(checkpoint=lambda: None, task={'short_id': short['id']})
    with pytest.raises(ValueError, match='acknowledgment'):
        collection_media.render_revision(ctx, revision, {}, tmp_path / 'media', 'export')
    assert not (tmp_path / 'media').exists()


def test_ack_request_is_strict_and_requires_keep(tmp_path):
    assert DecisionRequest(revision_id='r', rating='keep').acknowledge_warnings is False
    for invalid in [1, 'true', None]:
        with pytest.raises(ValidationError):
            DecisionRequest(revision_id='r', rating='keep', acknowledge_warnings=invalid)
    storage, _, short, revision = records(tmp_path, issues=warning())
    with pytest.raises(ValueError, match='Only a kept'):
        decide(storage, short, revision, rating='reject')
