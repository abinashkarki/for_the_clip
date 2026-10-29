"""Offset-only refinement preserves saved word evidence and independently reviewable meaning."""
import copy
import json

import pytest

from clippie import collection_editor as editor
from clippie.source_evidence import prepare_evidence
from clippie.word_spans import resolve_plan


@pytest.fixture
def material(tmp_path):
    source = {'id': 's', 'duration': 20, 'words': [
        {'id': f'private-word-{i}', 'text': text, 'start': i * .4, 'end': i * .4 + .3}
        for i, text in enumerate(['And', 'to', 'me,', 'this', 'is', 'a', 'claim.',
                                  'Another', 'complete', 'thought.'])]}
    evidence = prepare_evidence(source, tmp_path)
    ids = [u['id'] for u in evidence['artifact']['units']]
    return source, evidence, ids


def invoke(proposal, seen=None):
    def call(stage, prompt, schema, tokens, **kwargs):
        if seen is not None:
            seen.append((stage, json.loads(prompt.split('\n', 1)[1]), prompt, schema))
        return {'proposal': proposal, 'usage': {'total_tokens': 1}}
    return call


def plan(material, selections=None, constraints=None, base=None, **kwargs):
    source, evidence, ids = material
    return editor.plan_short({'id': 'idea', 'angle': 'A claim', 'unit_ids': ids}, [evidence], {'s': source},
        constraints or {}, invoke({'title': 'Claim', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.',
                                  'selections': selections or [{'unit_id': ids[0], 'start_word': 3, 'end_word': 6}]},
                                 kwargs.pop('seen', None)), base_plan=base, **kwargs)


def test_word_edge_compile_and_review_show_cut_speech_plus_original_qualification(material):
    source, evidence, ids = material
    seen = []
    result = plan(material, seen=seen)
    proof = result['word_spans'][ids[0]]
    assert proof['word_ids'] == [f'private-word-{i}' for i in range(3, 7)]
    assert proof['start'] == source['words'][3]['start'] and proof['end'] == source['words'][6]['end']
    assert proof['text'] == 'this is a claim.'
    projected = seen[0][1]['evidence'][0]
    assert projected['word_count'] == 7
    assert len(projected['head_choices']) == len(projected['tail_choices']) == 4
    assert projected['head_choices'][0] == {'index': 0, 'text': 'And'}
    assert 'private-word-' not in seen[0][2] and 'word_ids' not in seen[0][2]
    seen.clear()
    editor.review_short(result, [evidence], {'s': source}, {}, invoke({'status': 'needs_revision', 'issues': [
        {'category': 'qualification', 'severity': 'critical', 'detail': 'Personal qualifier omitted.', 'unit_ids': [ids[0]]}]}, seen))
    packet = seen[0][1]
    assert packet['output_order_speech'][0]['text'] == 'this is a claim.'
    assert packet['output_order_speech'][0]['start'] == proof['start']
    assert packet['original_trimmed_units'][0]['text'] == 'And to me, this is a claim.'
    assert packet['source_context_order'] == ids
    assert any('ASR' in warning for warning in packet['output_order_speech'][0]['warnings'])
    assert len(seen[0][2].encode()) + len(json.dumps(seen[0][3]).encode()) + 1024 <= 24576


@pytest.mark.parametrize('change', [
    {'start_word': True, 'end_word': 6}, {'start_word': 1.0, 'end_word': 6}, {'start_word': 1},
    {'end_word': 6}, {'start_word': -1, 'end_word': 6}, {'start_word': 4, 'end_word': 6},
    {'start_word': 0, 'end_word': 2}, {'start_word': 3, 'end_word': 2},
    {'start_word': 0, 'end_word': 7}, {'start': 1.2, 'end': 2.7},
])
def test_invalid_offsets_and_floating_time_injection_rejected(material, change):
    with pytest.raises(ValueError):
        plan(material, [{'unit_id': material[2][0], **change}])


@pytest.mark.parametrize('tamper', ['word_id', 'text', 'fingerprint', 'timeline', 'provenance'])
def test_forged_span_or_timeline_rejected_before_review_and_settings(material, tamper):
    source, evidence, ids = material
    result = plan(material)
    if tamper == 'word_id': result['word_spans'][ids[0]]['word_ids'][0] = 'invented'
    if tamper == 'text': result['word_spans'][ids[0]]['text'] = 'invented speech'
    if tamper == 'fingerprint': result['word_spans'][ids[0]]['source_fingerprint'] = 'invented'
    if tamper == 'timeline': result['timeline']['clips'][0]['start'] += .1
    if tamper == 'provenance': result['evidence_provenance']['sources']['s'] = 'invented'
    with pytest.raises(ValueError):
        editor.review_short(result, [evidence], {'s': source}, {}, lambda *args: pytest.fail('No provider on forged proof'))
    with pytest.raises(ValueError):
        editor.apply_settings(result, [evidence], {'s': source}, {'captions': False})


def test_overlapping_omitted_word_boundary_and_segment_only_rejected(tmp_path, material):
    source = copy.deepcopy(material[0])
    source['words'][2]['end'] = source['words'][3]['start'] + .1
    evidence = prepare_evidence(source, tmp_path / 'overlap')
    ids = [u['id'] for u in evidence['artifact']['units']]
    with pytest.raises(ValueError, match='Overlapping'):
        plan((source, evidence, ids))
    source = {'id': 's', 'duration': 10, 'segments': [{'start': 0, 'end': 5, 'text': 'A complete sentence.'}]}
    evidence = prepare_evidence(source, tmp_path / 'segment')
    ids = [u['id'] for u in evidence['artifact']['units']]
    with pytest.raises(ValueError, match='saved source words'):
        plan((source, evidence, ids), [{'unit_id': ids[0], 'start_word': 1, 'end_word': 2}])


def test_locked_trim_inherits_and_settings_never_expand_it(material):
    source, evidence, ids = material
    base = plan(material)
    constraints = {'locked_unit_ids': [ids[0]], 'canvas_style': 'blurred'}
    seen = []
    successor = plan(material, [{'unit_id': ids[0]}], constraints, base, seen=seen)
    assert seen[0][1]['base_word_spans'] == {ids[0]: {'start_word': 3, 'end_word': 6}}
    assert 'private-word-' not in seen[0][2] and 'source_fingerprint' not in seen[0][2]
    assert successor['word_spans'] == base['word_spans']
    assert successor['timeline']['clips'] == base['timeline']['clips']
    with pytest.raises(ValueError, match='Locked evidence word span'):
        plan(material, [{'unit_id': ids[0], 'start_word': 2, 'end_word': 6}], constraints, base)
    updated = editor.apply_settings(successor, [evidence], {'s': source},
                                    {'captions': False, 'caption_style': 'phrase', 'canvas_style': 'plain'})
    assert updated['word_spans'] == base['word_spans']
    assert updated['timeline']['clips'] == base['timeline']['clips']
    assert resolve_plan(updated, [evidence])[0]['text'] == 'this is a claim.'
    whole = plan(material, [{'unit_id': ids[0]}])
    assert 'word_spans' not in whole
    assert whole['timeline']['clips'][0]['start'] == evidence['artifact']['units'][0]['start']
    with pytest.raises(ValueError, match='Locked evidence word span'):
        plan(material, constraints=constraints, base=whole)


def test_trimmed_supporting_picture_positions_and_durations_follow_resolved_speech(material):
    source, evidence, ids = material
    sample = {'id': 'still', 'source_id': 's', 'at': 5., 'start': 4., 'end': 6.,
              'artifact_key': 'cached', 'media_identity': {'hash': 'abc'},
              'picture': {'id': 'still', 'at': 5., 'source': 's', 'mime_type': 'image/jpeg', 'data': 'aGVsbG8='}}
    selection = [{'unit_id': ids[0], 'start_word': 3, 'end_word': 6}, {'unit_id': ids[1]}]
    proposal = {'title': 'Claim', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': selection,
                'supporting_pictures': [{'sample_id': 'still', 'over_unit_id': ids[1]}]}
    result = editor.plan_short({'angle': 'Claim', 'unit_ids': ids}, [evidence], {'s': source}, {},
                               invoke(proposal), visual_samples=[sample])
    first = result['timeline']['clips'][0]
    shot = result['timeline']['broll'][0]
    assert shot['at'] == pytest.approx(first['end'] - first['start'])
    second = result['timeline']['clips'][1]
    assert shot['end'] - shot['start'] == pytest.approx(second['end'] - second['start'])
    editor.review_short(result, [evidence], {'s': source}, {}, invoke({'status': 'pass', 'issues': []}))


def test_missing_duplicate_source_word_ids_fail_closed(material):
    result = plan(material)
    evidence = copy.deepcopy(material[1])
    evidence['artifact']['transcript']['words'].pop(3)
    with pytest.raises(ValueError): resolve_plan(result, [evidence])
    evidence = copy.deepcopy(material[1])
    evidence['artifact']['transcript']['words'].append(evidence['artifact']['transcript']['words'][0])
    with pytest.raises(ValueError): resolve_plan(result, [evidence])


def test_api_transcript_projection_uses_saved_trim_and_fails_closed_on_forgery(material, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from clippie import collection_api
    _, evidence, ids = material
    result = plan(material)
    root = tmp_path / 'api'
    directory = root / 'source-evidence'
    directory.mkdir(parents=True)
    path = directory / f'evidence-{evidence["artifact"]["manifest"]["fingerprint"]}.json'
    path.write_text(json.dumps(evidence['artifact']))
    monkeypatch.setattr(collection_api, 'store', lambda: SimpleNamespace(root=root))
    revision = {'document': {'plan': result}}
    transcript = collection_api.revision_transcript(revision)
    assert transcript[0]['text'] == 'this is a claim.'
    assert transcript[0]['start'] == result['timeline']['clips'][0]['start']
    result['word_spans'][ids[0]]['text'] = 'forged'
    assert collection_api.revision_transcript(revision)[0]['unavailable'] is True
