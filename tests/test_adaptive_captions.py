"""Caption suggestions remain concrete, independent and subordinate to explicit user pins."""
import copy
import json

import pytest
from pydantic import ValidationError

from clippie import collection_editor as editor, collection_preferences as prefs
from clippie.collection_api import Constraints, ImproveRequest, SettingsRequest
from clippie.collection_store import CollectionStore
from tests.test_word_edge_refinement import material
from tests.test_collection_generation import pipeline, wait
from tests.test_collection_more import extra_provider, request
from tests.test_collection_preferences import setup


def make_plan(material, chosen='phrase', requested='auto', reason='Steady phrases keep this explanation readable.', seen=None):
    source, evidence, ids = material
    def invoke(stage, prompt, schema, tokens, **kwargs):
        if seen is not None: seen.append((json.loads(prompt.split('\n', 1)[1]), prompt, schema))
        return {'proposal': {'title': 'Supported moment', 'form': 'moment', 'edit_format': 'explanation',
                            'format_reason': 'The source explains one claim.', 'caption_style': chosen,
                            'caption_reason': reason, 'selections': [{'unit_id': ids[0]}]}}
    return editor.plan_short({'angle': 'One claim', 'unit_ids': ids[:1]}, [evidence], {'s': source},
                              {'caption_style': requested}, invoke)


@pytest.mark.parametrize('chosen', ['phrase', 'clean_focus', 'editorial'])
def test_auto_can_choose_each_style_for_same_format_and_review_gets_untrusted_choice(material, chosen):
    source, evidence, _ = material
    seen = []
    plan = make_plan(material, chosen=chosen, seen=seen)
    assert plan['edit_format'] == 'explanation'
    assert plan['constraints']['caption_style'] == 'auto'
    assert plan['timeline']['caption_style'] == chosen
    assert plan['caption_choice'] == {'style': chosen, 'reason': 'Steady phrases keep this explanation readable.', 'origin': 'model'}
    assert {'caption_style', 'caption_reason'} <= set(seen[0][2]['required'])
    assert seen[0][2]['properties']['caption_reason']['maxLength'] == 300
    assert 'semantic keywords' in seen[0][0]['task'] and 'unknown delivery' in seen[0][0]['task']
    assert len(seen[0][1].encode()) + len(json.dumps(seen[0][2]).encode()) + 1024 <= 24576
    packets = []
    def review(stage, prompt, schema, tokens, **kwargs):
        packets.append(json.loads(prompt.split('\n', 1)[1]))
        return {'proposal': {'status': 'pass', 'issues': []}}
    editor.review_short(plan, [evidence], {'s': source}, plan['constraints'], review)
    assert packets[0]['caption_choice'] == plan['caption_choice']
    assert 'untrusted' in packets[0]['task'] and packets[0]['output_order_speech'][0]['text']


def test_explicit_match_and_invalid_choice_reason_or_timeline_rejected(material):
    assert make_plan(material, requested='phrase')['caption_choice']['origin'] == 'user'
    with pytest.raises(ValueError, match='match an explicit'):
        make_plan(material, chosen='editorial', requested='phrase')
    for bad in ['auto', 'semantic_keywords', None, True]:
        with pytest.raises(ValueError): make_plan(material, chosen=bad)
    for bad in ['', None, 'x' * 301]:
        with pytest.raises(ValueError): make_plan(material, reason=bad)
    source, evidence, _ = material
    for change in ['timeline', 'origin', 'reason']:
        plan = make_plan(material)
        if change == 'timeline': plan['timeline']['caption_style'] = 'clean_focus'
        if change == 'origin': plan['caption_choice']['origin'] = 'user'
        if change == 'reason': plan['caption_choice']['reason'] = ''
        with pytest.raises(ValueError):
            editor.review_short(plan, [evidence], {'s': source}, plan['constraints'], lambda *args: pytest.fail('No inference on invalid caption proof'))
        with pytest.raises(ValueError):
            editor.apply_settings(plan, [evidence], {'s': source}, {'captions': False})


def test_local_unrelated_edits_and_off_preserve_auto_resolved_choice_manual_style_pins(material):
    source, evidence, _ = material
    plan = make_plan(material)
    original = copy.deepcopy(plan)
    for settings in [{'canvas_style': 'blurred'}, {'framing': {'mode': 'fit', 'zoom': 1, 'x': .2, 'y': .8}},
                     {'captions': False}]:
        plan = editor.apply_settings(plan, [evidence], {'s': source}, settings)
        assert plan['constraints']['caption_style'] == 'auto'
        assert plan['caption_choice'] == original['caption_choice']
        assert plan['timeline']['caption_style'] == 'phrase'
        assert plan['word_spans'] == original['word_spans'] if 'word_spans' in original else 'word_spans' not in plan
        assert [(c['start'], c['end']) for c in plan['timeline']['clips']] == [(c['start'], c['end']) for c in original['timeline']['clips']]
    pinned = editor.apply_settings(plan, [evidence], {'s': source}, {'caption_style': 'editorial'})
    assert pinned['constraints']['caption_style'] == pinned['timeline']['caption_style'] == 'editorial'
    assert pinned['caption_choice'] == {'style': 'editorial', 'reason': 'Selected by user.', 'origin': 'user'}
    legacy = copy.deepcopy(original)
    legacy.pop('caption_choice'); legacy['evidence_provenance']['prompt_version'] = 7
    legacy['constraints']['caption_style'] = 'phrase'
    legacy = editor.apply_settings(legacy, [evidence], {'s': source}, {'canvas_style': 'plain'})
    assert 'caption_choice' not in legacy and legacy['timeline']['caption_style'] == 'phrase'
    with pytest.raises(ValueError): editor.apply_settings(plan, [evidence], {'s': source}, {'caption_style': 'auto'})


def test_new_protocol_requires_caption_response_and_api_keeps_local_style_concrete(material):
    source, evidence, ids = material
    with pytest.raises(ValueError, match='concrete'):
        editor.plan_short({'angle': 'One claim', 'unit_ids': ids[:1]}, [evidence], {'s': source}, {},
            lambda *args: {'proposal': {'title': 'Claim', 'form': 'moment', 'edit_format': 'explanation',
                                       'format_reason': 'Source explains.', 'selections': [{'unit_id': ids[0]}]}})
    # Discovery's compact-reference protocol advanced the editor version; the
    # caption contract remains required and new plans retain that provenance.
    assert editor.PROMPT_VERSION == 11
    assert make_plan(material)['evidence_provenance']['prompt_version'] == 11
    assert Constraints().caption_style == 'auto'
    assert ImproveRequest(instruction='Improve').caption_style is None
    assert ImproveRequest(instruction='Improve', caption_style='auto').caption_style == 'auto'
    with pytest.raises(ValidationError): SettingsRequest(base_revision='r', request_id='q', caption_style='auto')


def test_improve_pin_inherit_auto_and_more_requested_style(pipeline, monkeypatch):
    client, calls, _ = pipeline
    cid = client.post('/api/collections', json={'source_ids': ['s']}).json()['id']
    assert client.post(f'/api/collections/{cid}/generate', json={'calls': 24}).status_code == 202
    ready = wait(client, cid)
    first, sibling = ready['shorts'][:2]
    current = first
    route = f'/api/collections/{cid}/shorts/{first["id"]}'
    for chosen, expected_request, expected_origin in [('phrase', 'phrase', 'user'), (None, 'phrase', 'user'), ('auto', 'auto', 'model')]:
        body = {'base_revision': current['current_revision'], 'instruction': f'Preserve source; treatment {expected_request}'}
        if chosen is not None: body['caption_style'] = chosen
        assert client.post(route + '/improve', json=body).status_code == 202
        current = next(s for s in wait(client, cid)['shorts'] if s['id'] == first['id'])
        plan = current['revision']['document']['plan']
        assert plan['constraints']['caption_style'] == expected_request
        assert plan['caption_choice']['origin'] == expected_origin
        assert plan['timeline']['caption_style'] == plan['caption_choice']['style'] != 'auto'
        if chosen == 'phrase':
            extra_provider(monkeypatch, calls)
            assert request(client, cid, current, identity='caption-extra').status_code == 202
            result = wait(client, cid)
            new = next(s for s in result['shorts'] if s['id'] not in {s['id'] for s in ready['shorts']})
            new_plan = new['revision']['document']['plan']
            assert new_plan['constraints']['caption_style'] == 'phrase'
            assert new_plan['caption_choice']['style'] == 'phrase' and new_plan['caption_choice']['origin'] == 'user'
    assert next(s for s in wait(client, cid)['shorts'] if s['id'] == sibling['id'])['current_revision'] == sibling['current_revision']
    assert [stage for stage, _, _ in calls].count('map') == 1


def test_preference_auto_default_preserves_saved_explicit_override_and_reset(setup):
    storage, client = setup
    assert client.get('/api/collection-preferences').json()['effective']['caption_style'] == 'auto'
    result = client.patch('/api/collection-preferences', json={'expected_revision': 0, 'changes': {'caption_style': 'clean_focus'}})
    assert result.status_code == 200
    assert prefs.load(CollectionStore(storage.root))['effective']['caption_style'] == 'clean_focus'
    result = client.patch('/api/collection-preferences', json={'expected_revision': 1, 'reset_fields': ['caption_style']})
    assert result.json()['effective']['caption_style'] == 'auto'
