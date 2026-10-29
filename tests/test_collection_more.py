"""Bounded extra discovery contracts using synthetic evidence and fake provider calls."""
import json

import pytest

from test_collection_generation import judged, pipeline, previews, start, wait
from clippie import collection_generation as generation
from clippie.collection_api import store


def extra_provider(monkeypatch, calls, mode='distinct'):
    previous = generation.provider.invoke

    def invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        if ctx.task['stage'] != 'discover_more':
            return previous(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs)
        data = json.loads(prompt.split('\n', 1)[1])
        calls.append((stage, ctx.task['short_id'], data))
        units = data['topic_maps'][0]['topics'][0]['unit_ids']
        candidates = [] if mode == 'empty' else [{
            'id': 'extra-idea', 'title': 'Different mechanism', 'angle': 'Different mechanism',
            'form': 'moment', 'edit_format': 'explanation', 'hook_line': 'Different mechanism',
            'components': judged(),
            'unit_ids': units if mode == 'duplicate' else units[:1]}]
        return {'proposal': {'candidates': candidates, 'rejected': [], 'duplicate_groups': [],
                             'explanation': 'Synthetic extra discovery.'}, 'usage': {'total_tokens': 20}}
    monkeypatch.setattr(generation.provider, 'invoke', invoke)


def request(client, cid, short, identity='extra-1', **changes):
    return client.post(f'/api/collections/{cid}/shorts/{short["id"]}/more', json={
        'base_revision': short['current_revision'], 'request_id': identity, **changes})


def test_distinct_extra_preserves_siblings_history_and_budget(pipeline, monkeypatch):
    client, calls, _ = pipeline
    cid = start(client)
    ready = wait(client, cid)
    first = ready['shorts'][0]
    client.post(f'/api/collections/{cid}/shorts/{first["id"]}/decision', json={
        'revision_id': first['current_revision'], 'rating': 'keep', 'notes': 'Keep the explanation.'})
    extra_provider(monkeypatch, calls)
    before = len(calls)
    allowance = ready['budget']
    response = request(client, cid, first)
    assert response.status_code == 202, response.text
    result = wait(client, cid)
    assert len(result['shorts']) == 4
    assert result['budget'] == allowance
    assert [stage for stage, _, _ in calls[before:]] == ['discover', 'plan', 'review']
    assert all(next(s for s in result['shorts'] if s['id'] == old['id'])['current_revision'] == old['current_revision']
               for old in ready['shorts'])
    history = calls[before][2]['history']
    assert len(history) == 6
    assert any(h.get('decisions', [{}])[0].get('rating') == 'keep' for h in history if h.get('decisions'))
    operation = result['additional_discovery'][0]
    assert operation['status'] == 'completed' and operation['short_id']
    assert request(client, cid, first).status_code == 202
    assert len(calls) == before + 3
    assert request(client, cid, first, instruction='Changed reuse').status_code == 409


@pytest.mark.parametrize('mode', ['empty', 'duplicate'])
def test_zero_extra_is_clear_and_idempotent(pipeline, monkeypatch, mode):
    client, calls, _ = pipeline
    cid = start(client)
    ready = wait(client, cid)
    extra_provider(monkeypatch, calls, mode)
    before = len(calls)
    assert request(client, cid, ready['shorts'][0]).status_code == 202
    result = wait(client, cid)
    assert len(result['shorts']) == 3
    assert 'No distinct supported idea' in result['additional_discovery'][0]['explanation']
    assert 'short_id' not in result['additional_discovery'][0]
    assert len(calls) == before + 1
    assert result['budget'] == ready['budget']
    assert request(client, cid, ready['shorts'][0]).status_code == 202
    assert len(calls) == before + 1
    task = next(t for t in store().list_tasks(cid) if t['stage'] == 'discover_more')
    assert task['attempts'] == 1 and task['reservation']['calls'] == 1


def test_queued_more_busy_guard_and_stale_source(pipeline, monkeypatch):
    client, calls, _ = pipeline
    cid = start(client)
    ready = wait(client, cid)
    first = ready['shorts'][0]
    from clippie import collection_workflow, app
    # A short-level canvas override must outrank the original plain collection default.
    assert ready['constraints']['canvas_style'] == 'plain'
    response = client.post(f'/api/collections/{cid}/shorts/{first["id"]}/settings', json={
        'base_revision': first['current_revision'], 'request_id': 'canvas-override', 'canvas_style': 'blurred'})
    assert response.status_code == 202, response.text
    first = next(s for s in wait(client, cid)['shorts'] if s['id'] == first['id'])
    assert first['revision']['document']['plan']['constraints']['canvas_style'] == 'blurred'
    monkeypatch.setattr(collection_workflow, 'dispatch', lambda *args: None)
    assert request(client, cid, first).status_code == 202
    assert request(client, cid, first).status_code == 202
    assert request(client, cid, first, identity='extra-2').status_code == 409
    assert len([t for t in store().list_tasks(cid) if t['stage'] == 'discover_more']) == 1
    more = next(t for t in store().list_tasks(cid) if t['stage'] == 'discover_more')
    assert more['payload']['constraints']['canvas_style'] == 'blurred'
    source = app.STORE.get('s')
    source['words'][0]['text'] = 'Changed.'
    app.STORE.save(source)
    assert request(client, cid, first, identity='stale').status_code == 409


def test_stale_source_rejected_before_extra_enqueue(pipeline):
    client, _, _ = pipeline
    cid = start(client)
    ready = wait(client, cid)
    from clippie import app
    source = app.STORE.get('s')
    source['words'][0]['text'] = 'Changed evidence.'
    app.STORE.save(source)
    result = request(client, cid, ready['shorts'][0])
    assert result.status_code == 409
    assert 'changed' in result.json()['detail'].lower()
    assert not any(t['stage'] == 'discover_more' for t in store().list_tasks(cid))
