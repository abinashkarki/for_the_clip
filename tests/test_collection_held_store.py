"""Held artifacts remain inspectable history without promoting or bypassing editorial holds."""
import pytest

from clippie.collection_store import CollectionStore


def setup(tmp_path):
    storage = CollectionStore(tmp_path)
    collection = storage.create_collection(['s'])
    short = storage.add_short(collection['id'])
    return storage, collection, short


def held(storage, short, previous=None):
    return storage.append_revision(short['id'], {'inspection_only': True, 'technical': 'pass',
        'editorial': {'status': 'needs_revision', 'issues': [{'severity': 'critical'}]}},
        expected_revision=previous, promote=False)


def task(storage, c, short, identity='preview'):
    storage.enqueue_task(c['id'], 'render_preview', identity, identity, short_id=short['id'])
    return storage.claim_task(collection_id=c['id'])


def commit(storage, short, revision, claim, kind='preview'):
    return storage.commit_artifact(short['id'], revision['id'], kind, {'video_path': '/qa.mp4'},
        claim['id'], claim['lease_token'], claim['input_version'])


def test_held_preview_never_promotes_and_old_late_commit_cannot_replace_new_hold(tmp_path):
    storage, c, short = setup(tmp_path)
    good = storage.append_revision(short['id'], {'technical': 'pass', 'editorial': {'status': 'pass'}})
    first = held(storage, short, good['id'])
    claim = task(storage, c, short)
    second = held(storage, short, good['id'])
    old_artifact = commit(storage, short, first, claim)
    state = storage.get_short(short['id'])
    assert state['current_revision'] == good['id'] and state['held_revision'] == second['id']
    assert not state.get('held_preview') and not state.get('pending_revision')
    assert storage.get_artifact(old_artifact['id'])['revision_id'] == first['id']
    with pytest.raises(ValueError, match='no longer current'):
        storage.save_decision(short['id'], first['id'], 'reject', expected_revision=first['id'])
    newclaim = task(storage, c, short, 'second-preview')
    preview = commit(storage, short, second, newclaim)
    assert storage.get_short(short['id'])['held_preview'] == preview['id']
    with pytest.raises(ValueError):
        commit(storage, short, second, newclaim, 'export')
    successor = storage.append_revision(short['id'], {'technical': 'pass', 'editorial': {'status': 'pass'}},
                                        expected_revision=good['id'], promote=False)
    state = storage.get_short(short['id'])
    assert state['pending_revision'] == successor['id'] and not state.get('held_revision')
    assert commit(storage, short, second, newclaim) == preview  # Immutable existing artifact.
    assert not storage.get_short(short['id']).get('held_preview')


def test_held_decisions_cancel_lease_and_restore_guards(tmp_path):
    storage, c, short = setup(tmp_path)
    revision = held(storage, short)
    for rating in ['revise', 'reject']:
        assert storage.save_decision(short['id'], revision['id'], rating,
                                    expected_revision=revision['id'])['rating'] == rating
    for rating, acknowledge in [('keep', False), ('revise', True)]:
        with pytest.raises(ValueError, match='only Revise or Reject'):
            storage.save_decision(short['id'], revision['id'], rating, expected_revision=revision['id'],
                                  acknowledge_warnings=acknowledge)
    with pytest.raises(ValueError, match='Restoration requires'):
        storage.restore_revision(c['id'], short['id'], revision['id'], None, 'held-restore')
    claim = task(storage, c, short)
    storage.recover_expired(now=claim['lease_until'])
    with pytest.raises(ValueError, match='Stale'):
        commit(storage, short, revision, claim)
    active = storage.claim_task(collection_id=c['id'])
    storage.cancel_collection(c['id'])
    with pytest.raises(ValueError, match='cancelled'):
        commit(storage, short, revision, active)
    assert storage.list_artifacts(short['id']) == []


def test_source_replacement_during_held_render_prevents_publication(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from clippie import collection_generation, collection_media, source_evidence
    from clippie.store import Store
    storage, c, short = setup(tmp_path / 'data')
    source_path = tmp_path / 'source.mp4'
    source_path.write_bytes(b'original media placeholder')
    sources = Store(tmp_path / 'data')
    source = sources.save({'id': 's', 'source_path': str(source_path), 'duration': 2,
                           'words': [{'id': 0, 'text': 'Original.', 'start': 0, 'end': 1}]})
    evidence = source_evidence.prepare_evidence(source, storage.root / 'source-evidence')['artifact']
    revision = storage.append_revision(short['id'], {'inspection_only': True, 'technical': 'pass',
        'editorial': {'status': 'uncertain', 'issues': []},
        'plan': {'evidence_provenance': {'sources': {'s': evidence['manifest']['fingerprint']}}}}, promote=False)
    run = storage.start_run(c['id'], {'constraints': {}, 'deadline': time.time() + 60}, 'source-fence')
    storage.enqueue_task(c['id'], 'render_preview', 'media', 'media', short_id=short['id'],
                          payload={'revision_id': revision['id']})
    claim = storage.claim_task(collection_id=c['id'])
    def replaced_during_render(*args, **kwargs):
        source_path.write_bytes(b'replaced source bytes must never be published')
        return {'video_path': '/not-published.mp4'}
    monkeypatch.setattr(collection_media, 'render_revision', replaced_during_render)
    ctx = SimpleNamespace(task=claim)
    with pytest.raises(ValueError, match='original source media changed'):
        collection_generation._handlers(storage, sources, run)['render_preview'](ctx)
    assert storage.list_artifacts(short['id']) == []
    assert not storage.get_short(short['id']).get('held_preview')


def test_restore_good_saved_revision_clears_held_pointers_preserving_history(tmp_path):
    storage, c, short = setup(tmp_path)
    good = storage.append_revision(short['id'], {'technical': 'pass', 'editorial': {'status': 'pass'}})
    claim = task(storage, c, short, 'good-preview')
    good_artifact = commit(storage, short, good, claim)
    storage.finish_task(claim['id'], claim['lease_token'], claim['input_version'])
    inspection = held(storage, short, good['id'])
    claim = task(storage, c, short, 'held-preview')
    held_artifact = commit(storage, short, inspection, claim)
    storage.finish_task(claim['id'], claim['lease_token'], claim['input_version'])
    restored = storage.restore_revision(c['id'], short['id'], good['id'], good['id'], 'restore-good')
    assert restored['current_revision'] == good['id'] and restored['current_preview'] == good_artifact['id']
    assert not restored.get('held_revision') and not restored.get('held_preview')
    assert storage.get_artifact(held_artifact['id'])['revision_id'] == inspection['id']
    assert len(storage.list_revisions(short['id'])) == 2
