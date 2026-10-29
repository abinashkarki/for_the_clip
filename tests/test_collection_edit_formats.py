"""Source-grounded format steering is independent of structural form and visual profiles."""
import copy
import json

import pytest
from pydantic import ValidationError

from clippie import collection_editor as editor, collection_preferences as prefs
from clippie.collection_api import Constraints, ImproveRequest
from tests.test_word_edge_refinement import material
from tests.test_collection_generation import pipeline, wait
from tests.test_collection_more import extra_provider, request
from tests.test_collection_preferences import setup


def make_plan(material, chosen='explanation', requested='auto', reason='Source supplies a complete explanation.', seen=None):
    source, evidence, ids = material
    def invoke(stage, prompt, schema, tokens, **kwargs):
        if seen is not None: seen.append((json.loads(prompt.split('\n', 1)[1]), prompt, schema))
        return {'proposal': {'title': 'Supported moment', 'form': 'moment', 'edit_format': chosen,
                            'format_reason': reason, 'caption_style': 'clean_focus', 'caption_reason': 'Readable captions for this synthetic source.', 'selections': [{'unit_id': ids[0]}]}}
    return editor.plan_short({'angle': 'One supported idea', 'unit_ids': ids[:1]}, [evidence], {'s': source},
                              {'edit_format': requested}, invoke)


@pytest.mark.parametrize('chosen', ['explanation', 'small_story', 'exchange'])
def test_auto_resolves_per_short_independently_of_form_and_review_sees_fit(material, chosen):
    source, evidence, _ = material
    seen = []
    plan = make_plan(material, chosen=chosen, seen=seen)
    assert plan['edit_format'] == chosen and plan['form'] == 'moment'
    assert plan['constraints']['edit_format'] == 'auto'
    assert seen[0][2]['properties']['edit_format']['enum'] == sorted(editor.EDIT_FORMATS)
    assert {'edit_format', 'format_reason'} <= set(seen[0][2]['required'])
    assert 'manufacture' in seen[0][0]['task'] and 'audience' in seen[0][0]['task']
    packet = []
    def review(stage, prompt, schema, tokens, **kwargs):
        packet.append(json.loads(prompt.split('\n', 1)[1]))
        return {'proposal': {'status': 'pass', 'issues': []}}
    editor.review_short(plan, [evidence], {'s': source}, plan['constraints'], review)
    assert packet[0]['edit_format'] == chosen and packet[0]['format_reason'] == plan['format_reason']
    assert 'earned' in packet[0]['task'] and packet[0]['output_order_speech']
    assert len(seen[0][1].encode()) + len(json.dumps(seen[0][2]).encode()) + 1024 <= 24576


def test_explicit_format_match_mismatch_missing_reason_and_old_local_settings(material):
    plan = make_plan(material, chosen='exchange', requested='exchange')
    with pytest.raises(ValueError, match='requested edit format'):
        make_plan(material, chosen='explanation', requested='exchange')
    for bad in ['auto', 'viral', None]:
        with pytest.raises(ValueError): make_plan(material, chosen=bad)
    for bad in ['', 'x' * 401, None]:
        with pytest.raises(ValueError): make_plan(material, reason=bad)
    source, evidence, _ = material
    updated = editor.apply_settings(plan, [evidence], {'s': source}, {'caption_style': 'phrase', 'canvas_style': 'blurred'})
    assert updated['edit_format'] == 'exchange' and updated['format_reason'] == plan['format_reason']
    legacy = copy.deepcopy(plan)
    legacy.pop('edit_format'); legacy.pop('format_reason'); legacy.pop('caption_choice')
    legacy['constraints'].pop('edit_format')
    legacy['evidence_provenance']['prompt_version'] = 6
    old = editor.apply_settings(legacy, [evidence], {'s': source}, {'captions': False})
    assert 'edit_format' not in old and 'format_reason' not in old
    seen = []
    editor.review_short(old, [evidence], {'s': source}, {}, lambda stage, prompt, *args, **kwargs:
        (seen.append(json.loads(prompt.split('\n', 1)[1])) or {'proposal': {'status': 'pass', 'issues': []}}))
    assert 'edit_format' not in seen[0] and 'format_reason' not in seen[0]


def test_new_protocol_cannot_omit_format_fields(material):
    source, evidence, ids = material
    with pytest.raises(ValueError, match='resolved format'):
        editor.plan_short({'angle': 'Idea', 'unit_ids': ids[:1]}, [evidence], {'s': source}, {},
            lambda *args: {'proposal': {'title': 'Idea', 'form': 'moment', 'selections': [{'unit_id': ids[0]}]}})
    assert editor.PROMPT_VERSION >= 9
    assert Constraints().edit_format == 'auto' and ImproveRequest(instruction='Improve').edit_format is None
    with pytest.raises(ValidationError): Constraints(edit_format='viral')
    with pytest.raises(ValidationError): ImproveRequest(instruction='Improve', edit_format='viral')


def test_improve_override_omission_auto_and_more_like_inheritance(pipeline, monkeypatch):
    client, calls, _ = pipeline
    cid = client.post('/api/collections', json={'source_ids': ['s']}).json()['id']
    assert client.post(f'/api/collections/{cid}/generate', json={'calls': 24}).status_code == 202
    ready = wait(client, cid)
    first, sibling = ready['shorts'][:2]
    route = f'/api/collections/{cid}/shorts/{first["id"]}'
    current = first
    for choice, expected_request, expected_chosen in [('small_story', 'small_story', 'small_story'),
                                                     (None, 'small_story', 'small_story'),
                                                     ('auto', 'auto', 'explanation')]:
        body = {'base_revision': current['current_revision'], 'instruction': f'Preserve evidence: {expected_request}'}
        if choice is not None: body['edit_format'] = choice
        assert client.post(route + '/improve', json=body).status_code == 202
        current = next(s for s in wait(client, cid)['shorts'] if s['id'] == first['id'])
        plan = current['revision']['document']['plan']
        assert plan['constraints']['edit_format'] == expected_request
        assert plan['edit_format'] == expected_chosen
        if choice == 'small_story':
            extra_provider(monkeypatch, calls)
            assert request(client, cid, current, identity='format-extra').status_code == 202
            more = wait(client, cid)
            new_short = next(s for s in more['shorts'] if s['id'] not in {old['id'] for old in ready['shorts']})
            new_plan = new_short['revision']['document']['plan']
            assert new_plan['constraints']['edit_format'] == 'small_story'
            assert new_plan['edit_format'] == 'small_story'
    final = wait(client, cid)
    assert next(s for s in final['shorts'] if s['id'] == sibling['id'])['current_revision'] == sibling['current_revision']
    assert [stage for stage, _, _ in calls].count('map') == 1


def test_format_preferences_conflict_scoped_reset_and_defaults(setup):
    storage, client = setup
    result = client.patch('/api/collection-preferences', json={
        'expected_revision': 0, 'changes': {'edit_format': 'exchange', 'canvas_style': 'blurred'}})
    assert result.status_code == 200
    assert prefs.load(storage)['effective']['edit_format'] == 'exchange'
    assert client.patch('/api/collection-preferences', json={
        'expected_revision': 0, 'changes': {'edit_format': 'explanation'}}).status_code == 409
    assert client.patch('/api/collection-preferences', json={
        'expected_revision': 1, 'changes': {'edit_format': 'exchange'}, 'reset_fields': ['edit_format']}).status_code == 422
    result = client.patch('/api/collection-preferences', json={
        'expected_revision': 1, 'reset_fields': ['edit_format']}).json()
    assert result['effective']['edit_format'] == 'auto' and result['effective']['canvas_style'] == 'blurred'
    assert len(prefs.DEFAULTS) == 9
