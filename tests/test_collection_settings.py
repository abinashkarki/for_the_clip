"""Local settings preserve immutable grounded speech and never invoke a provider."""
import copy

import pytest
from pydantic import ValidationError

from clippie import app as module
from clippie.collection_api import SettingsRequest, store
from tests.test_collection_generation import pipeline, previews, start, wait


@pytest.mark.parametrize('change', [
    {'framing': {'mode': 'fill', 'zoom': True, 'x': .5, 'y': .5}},
    {'framing': {'mode': 'fill', 'zoom': 1.6, 'x': .5, 'y': .5}},
    {'framing': {'mode': 'fit', 'zoom': 1, 'x': float('nan'), 'y': .5}},
    {'captions': 'false'}, {'caption_style': 'invented'}, {'extra': True},
    {'framing': {'mode': 'fill', 'zoom': 1, 'x': .5, 'y': .5, 'extra': 1}},
])
def test_strict_settings(change):
    with pytest.raises(ValidationError):
        SettingsRequest(base_revision='r', request_id='q', **change)


@pytest.mark.parametrize('style,captions,canvas', [('editorial', False, 'plain'), ('phrase', True, 'blurred')])
def test_local_settings_render_and_subsequent_suggestion_preserve_settings(pipeline, monkeypatch, style, captions, canvas):
    client, calls, _ = pipeline
    cid = start(client)
    ready = previews(client, cid)
    short, sibling = ready['shorts'][:2]
    sid = short['id']
    old_revision = copy.deepcopy(short['revision'])
    old_url = short['preview']['video_url']
    ids = old_revision['document']['plan']['unit_ids']
    before = len(calls)
    monkeypatch.setitem(module.KEYS, 'gemini', '')
    geometry = {'mode': 'fill', 'zoom': 1.2, 'x': .2, 'y': .7}
    override = {'mode': 'fill', 'zoom': 1.4, 'x': .8, 'y': .3}
    endpoint = f'/api/collections/{cid}/shorts/{sid}/settings'
    body = {'base_revision': short['current_revision'], 'request_id': 'visual1',
            'captions': captions, 'caption_style': style, 'canvas_style': canvas, 'framing': geometry,
            'shot_framing': {ids[1]: override}}
    assert client.post(endpoint, json={**body, 'shot_framing': {'fake': override}}).status_code == 409
    response = client.post(endpoint, json=body)
    assert response.status_code == 202, response.text
    completed = wait(client, cid)
    updated = next(s for s in completed['shorts'] if s['id'] == sid)
    assert len(calls) == before
    assert updated['current_revision'] != short['current_revision']
    document = updated['revision']['document']
    assert document['human_review'] == 'unreviewed'
    assert document['timeline'] == document['plan']['timeline']
    assert [c['framing'] for c in document['timeline']['clips']] == [geometry, override]
    assert document['timeline']['captions'] is captions
    assert document['timeline']['caption_style'] == style
    assert document['timeline']['canvas_style'] == canvas
    assert document['plan']['unit_ids'] == ids
    for old, new in zip(old_revision['document']['timeline']['clips'], document['timeline']['clips']):
        assert [(old[k], new[k]) for k in ('source_id', 'start', 'end')] == [(old[k], old[k]) for k in ('source_id', 'start', 'end')]
    assert document['editorial']['carried_review']['base_revision'] == short['current_revision']
    tasks = [t for t in store().list_tasks(cid) if t['stage'] == 'apply_settings']
    assert len(tasks) == 1 and tasks[0]['reservation'] == {'tokens': 0, 'calls': 0}
    assert client.get(old_url).status_code == 200
    assert next(s for s in completed['shorts'] if s['id'] == sibling['id'])['current_revision'] == sibling['current_revision']
    assert client.post(endpoint, json=body).status_code == 202
    assert client.post(endpoint, json={**body, 'captions': not captions}).status_code == 409
    assert client.post(endpoint, json={**body, 'request_id': 'stale'}).status_code == 409
    assert store().get_revision(old_revision['id']) == old_revision
    monkeypatch.setitem(module.KEYS, 'gemini', 'fake-test-key')
    improved = client.post(f'/api/collections/{cid}/shorts/{sid}/improve', json={
        'base_revision': updated['current_revision'], 'instruction': 'Preserve speech', 'locked_unit_ids': ids})
    assert improved.status_code == 202, improved.text
    after = next(s for s in wait(client, cid)['shorts'] if s['id'] == sid)
    constraints = after['revision']['document']['plan']['constraints']
    assert constraints['shot_framing'] == {ids[1]: override}
    assert constraints['framing'] == geometry and constraints['captions'] is captions
    assert constraints['caption_style'] == style
    assert constraints['canvas_style'] == canvas
    assert constraints['locked_unit_ids'] == ids


def test_caption_only_preserves_legacy_mixed_crops(tmp_path):
    from clippie import collection_editor as editor
    from clippie.source_evidence import prepare_evidence
    source = {'id': 's', 'duration': 10, 'segments': [
        {'start': 0, 'end': 2, 'text': 'First.'}, {'start': 3, 'end': 5, 'text': 'Second.'}]}
    evidence = prepare_evidence(source, tmp_path)
    ids = [u['id'] for u in evidence['artifact']['units']]
    base = editor.plan_short({'unit_ids': ids, 'angle': 'Test'}, [evidence], {'s': source}, {},
        lambda *args: {'proposal': {'title': 'Test', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': [{'unit_id': i} for i in ids]}})
    mixed = {'mode': 'fill', 'zoom': 1.3, 'x': .1, 'y': .7}
    base['timeline']['clips'][1]['framing'] = mixed
    updated = editor.apply_settings(base, [evidence], {'s': source}, {'captions': False})
    assert updated['timeline']['clips'] == base['timeline']['clips']
    assert updated['constraints']['shot_framing'] == {ids[1]: mixed}


def test_atomic_settings_enqueue_fences_race_and_restart(tmp_path):
    from clippie.collection_store import CollectionStore
    storage = CollectionStore(tmp_path)
    cid = storage.create_collection(['source'])['id']
    sid = storage.add_short(cid)['id']
    base = storage.append_revision(sid, {})['id']
    payload = {'base_revision': base, 'settings': {'captions': False}}
    first = storage.enqueue_task(cid, 'apply_settings', 'v', 'q', short_id=sid, payload=payload)
    reopened = CollectionStore(tmp_path)
    assert reopened.enqueue_task(cid, 'apply_settings', 'v', 'q', short_id=sid, payload=payload)['id'] == first['id']
    with pytest.raises(ValueError, match='processing'):
        reopened.enqueue_task(cid, 'apply_settings', 'v', 'q2', short_id=sid, payload=payload)
    with pytest.raises(ValueError, match='changed'):
        reopened.enqueue_task(cid, 'apply_settings', 'v', 'q3', short_id=sid,
                              payload={**payload, 'base_revision': 'stale'})
    claimed = reopened.claim_task()
    saved = reopened.append_revision(sid, {}, expected_revision=base, task_id=claimed['id'],
        lease_token=claimed['lease_token'], input_version='v', promote=False)
    from clippie.collection_runner import run_one
    reopened.recover_expired(now=claimed['lease_until'] + 1)
    result = run_one(reopened, {}, collection_id=cid)
    assert result['result']['revision_id'] == saved['id']
    assert len(reopened.list_revisions(sid)) == 2
    assert reopened.get_short(sid)['current_revision'] == base
