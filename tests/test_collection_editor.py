"""Synthetic mechanics tests; these do not measure editorial quality."""
import json
import pytest
from clippie import collection_editor as editor
from clippie.source_evidence import prepare_evidence


@pytest.fixture
def material(tmp_path):
    source = {'id': 's', 'duration': 2000, 'segments': [
        {'start': i*10, 'end': i*10+8, 'text': text} for i, text in enumerate([
            'The question is whether it always works.', 'It works only when water is available.',
            'Without water it fails.', 'An unrelated memory.', 'Another unrelated aside.',
            'The later useful idea is storage.', 'Storage shifts supply into the evening.'])]}
    evidence = prepare_evidence(source, tmp_path, 100)
    ids = [u['id'] for u in evidence['artifact']['units']]
    return source, evidence, ids


def invoke_for(proposal, seen=None):
    def invoke(stage, prompt, schema, max_tokens, **kwargs):
        if seen is not None:
            seen.append((stage, prompt, schema, max_tokens))
        return {'proposal': proposal, 'usage': {'total_tokens': 42}}
    return invoke


def test_visual_observations_require_attached_sample_and_respect_request_limit(material):
    _, evidence, _ = material
    packet = evidence['artifact']['packets'][0]
    picture = {'id': 'still1', 'source': 's', 'at': 1., 'data': 'a' * 1000}
    captured = []
    def invoke(stage, prompt, schema, tokens, **kwargs):
        captured.append((prompt, kwargs))
        return {'proposal': {'topics': [], 'visual_notes': [{'sample_id': 'still1', 'observation': 'Blue picture; motion unknown.'}]}}
    mapped = editor.map_packet(packet, evidence, invoke, pictures=[picture])
    assert mapped['visual_sample_ids'] == ['still1'] and mapped['visual_omitted_count'] == 0
    assert captured[0][1]['pictures'] == [picture]
    assert json.loads(captured[0][0].split('\n', 1)[1])['visual_samples'][0]['id'] == 'still1'
    # A note about a picture that was not attached is dropped, not trusted and not fatal.
    unattached = editor.map_packet(packet, evidence, invoke)
    assert unattached['visual_notes'] == [] and unattached['repairs'] == {'dropped_visual_notes': 1}
    content, selected = editor._visual_input({'text': 'x' * 23000}, {}, [picture])
    assert not selected and not content['visual_samples']


def judged(**scores):
    return {name: {'score': scores.get(name, 4), 'evidence': f'{name} evidence'}
            for name in ('hook', 'standalone', 'payoff', 'density', 'shareability')}


def candidate(ids):
    return {'id': 'a', 'title': 'Water', 'angle': 'Qualified water claim', 'form': 'moment', 'unit_ids': ids,
            'edit_format': 'explanation', 'hook_line': 'Water decides whether it works', 'components': judged()}


def plan(material, ids=None, constraints=None, seen=None):
    source, evidence, all_ids = material
    ids = ids or all_ids[:2]
    return editor.plan_short(candidate(ids), [evidence], {'s': source}, constraints or {},
        invoke_for({'title': 'Water', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': [{'unit_id': i} for i in ids]}, seen))


def test_packet_map_exact_refs_and_bounded_content(material):
    _, evidence, ids = material
    packet = evidence['artifact']['packets'][0]
    seen = []
    result = editor.map_packet(packet, evidence, invoke_for({'topics': [{'angle': 'Water qualification', 'unit_ids': packet['unit_ids']}]}, seen))
    assert result['kind'] == 'derived_topic_map'
    assert result['model_usage']['total_tokens'] == 42
    assert ids[-1] not in seen[0][1]
    # Maps are hints: a mistyped ID is dropped (never matched to a near miss) and the packet still maps.
    good, typo = packet['unit_ids'][0], packet['unit_ids'][0][:-3] + '7' + packet['unit_ids'][0][-3:]
    mapped = editor.map_packet(packet, evidence, invoke_for({'topics': [
        {'angle': 'Invented', 'unit_ids': ['fake']}, {'angle': 'Real', 'unit_ids': [typo, good, good]}]}))
    assert mapped['topics'] == [{'angle': 'Real', 'unit_ids': [good]}]
    assert mapped['repairs'] == {'dropped_unit_ids': 3, 'dropped_topics': 1}
    with pytest.raises(ValueError, match='list of packet topics'):
        editor.map_packet(packet, evidence, invoke_for({'topics': 'none'}))


def test_discover_late_topics_semantic_duplicates_and_overlap(material):
    _, evidence, ids = material
    maps = [{'topics': [{'angle': 'Storage', 'unit_ids': ids[-2:]}]}]
    a = candidate(ids[:2]); b = {**candidate(ids[2:3]), 'id': 'b', 'angle': 'No water means no success'}
    c = {**candidate(ids[-2:]), 'id': 'c', 'angle': 'Evening storage'}
    seen = []
    result = editor.discover(maps, [evidence], {}, [], invoke_for({'candidates': [a,b,c],
        'rejected': [], 'duplicate_groups': [['a','b']], 'explanation': 'Two distinct claims.'}, seen))
    assert [c['id'] for c in result['candidates']] == ['a','c']
    assert result['rejected'][0]['id'] == 'b'
    assert [(c['id'], c['disposition']) for c in result['pool']] == [('a', 'selected'), ('b', 'rejected'), ('c', 'selected')]
    assert result['pool'][1]['rejection_reason'] == result['rejected'][0]['reason']
    assert result['source_overlap'][0]['shared_unit_ids'] == []
    sent = json.loads(seen[0][1].split('\n', 1)[1])
    assert sent['transcript'][-1][2] == evidence['artifact']['units'][-1]['text']
    assert sent['transcript'][-1][0] == f'e1_{len(ids) - 1}'
    zero = editor.discover(maps, [evidence], {}, [], invoke_for({'candidates': [], 'rejected': [], 'duplicate_groups': [], 'explanation': 'No supported distinct idea.'}))
    assert zero['candidates'] == []


def test_plan_exact_times_portrait_no_fixed_beats_and_target_context(material):
    _, _, ids = material
    seen = []
    result = plan(material, ids[:1], seen=seen)
    assert len(result['timeline']['clips']) == 1
    assert result['timeline']['schema_version'] == 2
    assert result['timeline']['width'] == 1080
    assert result['timeline']['duration'] == 8
    assert ids[1] in seen[0][1]  # exact neighbor qualification
    assert ids[-1] not in seen[0][1]
    assert 'timestamps' not in result


def test_scope_duration_locks_form_and_unknown_refs(material):
    _, _, ids = material
    for constraints in ({'source_roles': {'s': 'picture_only'}}, {'max_duration': 7},
                        {'locked_unit_ids': [ids[3]]}, {'form': 'story'}):
        with pytest.raises(ValueError):
            plan(material, ids[:1], constraints)
    source, evidence, _ = material
    with pytest.raises(ValueError, match='unknown'):
        editor.plan_short(candidate(ids[:1]), [evidence], {'s': source}, {},
            invoke_for({'title': 'No', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': [{'unit_id': 'invented'}]}))


def test_review_output_order_neighbors_independent_and_critical_cannot_pass(material):
    source, evidence, ids = material
    result = plan(material, ids[1:3])
    seen = []
    review = editor.review_short(result, [evidence], {'s': source}, {}, invoke_for({
        'status': 'pass', 'issues': [{'category': 'qualification', 'severity': 'critical',
        'detail': 'Opening omits question context.', 'unit_ids': [ids[0]]}]}, seen))
    assert review['status'] == 'needs_revision'
    prompt = json.loads(seen[0][1].split('\n', 1)[1])
    assert [u['id'] for u in prompt['output_order_speech']] == ids[1:3]
    assert 'candidate' not in prompt and 'angle' not in prompt
    assert prompt['source_context_order'] == ids[:4]
    assert not set(ids[1:3]) & {unit['id'] for unit in prompt['neighbor_context']}
    assert ids[0] in seen[0][1] and ids[-1] not in seen[0][1]
    result['timeline']['clips'][0]['start'] += 1
    with pytest.raises(ValueError, match='exact evidence'):
        editor.review_short(result, [evidence], {'s': source}, {}, invoke_for({'status': 'pass', 'issues': []}))


def test_prompt_bound_prevents_call(material):
    _, evidence, ids = material
    with pytest.raises(ValueError, match='bounded prompt'):
        editor.discover([{'topics': [{'angle': 'x'*130000, 'unit_ids': ids[:1]}]}], [evidence], {}, [],
                        lambda *a: pytest.fail('Oversize prompt must not invoke provider'))


def test_review_receives_headline_as_untrusted_content_and_holds_critical_issue(material):
    source, evidence, ids = material
    result = plan(material, ids[1:3])
    result['title'] = 'It always works, even without water'
    seen = []
    review = editor.review_short(result, [evidence], {'s': source}, {}, invoke_for({
        'status': 'pass', 'issues': [{'category': 'misleading_title', 'severity': 'critical',
        'detail': 'Headline contradicts the condition stated in the speech.', 'unit_ids': ids[1:3]}]}, seen))
    prompt = json.loads(seen[0][1].split('\n', 1)[1])
    assert prompt['proposed_title'] == result['title']
    assert 'candidate' not in prompt and 'angle' not in prompt
    assert [u['id'] for u in prompt['output_order_speech']] == ids[1:3]
    assert review['status'] == 'needs_revision'


def test_review_long_alignment_ids_do_not_overflow_transport(tmp_path):
    source = {'id': 's', 'duration': 60, 'words': [
        {'id': f'word-{i:045d}', 'text': 'detail.' if i in (139, 279) else 'detail',
         'start': i * .2, 'end': (i + 1) * .2} for i in range(280)], 'segments': []}
    evidence = prepare_evidence(source, tmp_path)
    ids = [unit['id'] for unit in evidence['artifact']['units']]
    result = plan((source, evidence, ids))
    seen = []
    review = editor.review_short(result, [evidence], {'s': source}, {},
                                invoke_for({'status': 'pass', 'issues': []}, seen))
    assert review['status'] == 'pass' and len(seen) == 1
    prompt = json.loads(seen[0][1].split('\n', 1)[1])
    assert [unit['text'] for unit in prompt['output_order_speech']] == [unit['text'] for unit in evidence['artifact']['units']]
    assert all(unit['speaker_status'] == 'unknown' for unit in prompt['output_order_speech'])
    assert all(unit['alignment'] == 'approximate' for unit in prompt['neighbor_context'])


def test_review_unknown_issue_reference_cannot_pass(material):
    source, evidence, _ = material
    result = plan(material)
    with pytest.raises(ValueError, match='unknown'):
        editor.review_short(result, [evidence], {'s': source}, {}, invoke_for({
            'status': 'pass', 'issues': [{'category': 'context', 'severity': 'warning',
            'detail': 'Unsupported issue pointer.', 'unit_ids': ['invented']}]}))


def test_replan_preserves_locked_order_and_manual_caption_framing(material):
    source, evidence, ids = material
    base = plan(material)
    constraints = {'locked_unit_ids': ids[:2], 'captions': False,
                   'framing': {'mode': 'fill', 'zoom': 1.1, 'x': .2, 'y': .6}}
    with pytest.raises(ValueError, match='order'):
        editor.plan_short(candidate(ids[:2]), [evidence], {'s': source}, constraints,
            invoke_for({'title': 'Water', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': [
                {'unit_id': ids[1]}, {'unit_id': ids[0]}]}), base_plan=base)
    result = plan(material, constraints=constraints)
    assert result['timeline']['captions'] is False
    assert result['timeline']['clips'][0]['framing']['x'] == .2


def test_discovery_weak_and_explicitly_rejected_never_selected(material):
    _, evidence, ids = material
    proposals = [{**candidate(ids[:1]), 'id': 'weak', 'components': judged(standalone=2)},
                 {**candidate(ids[1:2]), 'id': 'open', 'components': judged(payoff=1)},
                 {**candidate(ids[-2:]), 'id': 'reject'}]
    result = editor.discover([], [evidence], {}, [], invoke_for({'candidates': proposals,
        'rejected': [{'id': 'reject', 'reason': 'Excluded angle.'}], 'duplicate_groups': [],
        'explanation': 'No complete allowed idea remains.'}))
    assert result['candidates'] == []
    assert {r['id'] for r in result['rejected']} == {'weak', 'open', 'reject'}


def test_discovery_compacts_map_provenance_and_enforces_form(material):
    _, evidence, ids = material
    topic_map = {'source_id': 's', 'topics': [{'angle': 'Water', 'unit_ids': ids[:1]}],
                 'evidence_provenance': {'unit_ids': ['ignored-provenance']*100000},
                 'model_usage': {'ignored': 'x'*100000}}
    seen = []
    response = {'candidates': [candidate(ids[:1])], 'rejected': [], 'duplicate_groups': [], 'explanation': 'One complete idea.'}
    editor.discover([topic_map], [evidence], {}, [], invoke_for(response, seen))
    assert 'ignored-provenance' not in seen[0][1]
    assert len(seen[0][1]) < 5000
    story_only = editor.discover([topic_map], [evidence], {'form': 'story'}, [], invoke_for(response))
    assert story_only['candidates'] == [] and 'form' in story_only['rejected'][0]['reason']


def test_repair_instruction_has_targeted_exact_neighbors(material):
    source, evidence, ids = material
    seen = []
    editor.plan_short(candidate(ids[1:2]), [evidence], {'s': source}, {}, invoke_for({
        'title': 'Qualification', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': [{'unit_id': ids[1]}]}, seen),
        instruction=f'Preserve qualification in {ids[1]} and resolve missing question context.')
    prompt = json.loads(seen[0][1].split('\n', 1)[1])
    assert ids[1] in prompt['instruction']
    assert {u['id'] for u in prompt['evidence']} == set(ids[:3])
    assert ids[-1] not in seen[0][1]


def test_utf8_transport_bound_prevents_invoke(material):
    _, evidence, ids = material
    with pytest.raises(ValueError, match='bounded prompt'):
        editor.discover([{'topics': [{'angle': '漢'*45000, 'unit_ids': ids[:1]}]}], [evidence], {}, [],
                        lambda *a: pytest.fail('UTF8 transport overflow must not invoke'))


def test_map_seventy_fragment_packet_has_no_duplicate_transport(tmp_path):
    source = {'id': 'many', 'duration': 1000, 'segments': [
        {'start': i*2, 'end': i*2+1, 'text': f'Sentence {i:02d} ' + 'word '*13 + '.'}
        for i in range(70)]}
    evidence = prepare_evidence(source, tmp_path, 6000)
    packet = evidence['artifact']['packets'][0]
    assert len(packet['fragments']) == 70
    seen = []
    editor.map_packet(packet, evidence, invoke_for({'topics': []}, seen))
    _, prompt, schema, _ = seen[0]
    compact = json.loads(prompt.split('\n', 1)[1])['packet']
    assert set(compact) == {'id', 'source_id', 'fragments'}
    assert set(compact['fragments'][0]) <= {'unit_id', 'text', 'partial_unit', 'text_start', 'text_end'}
    assert len(prompt.encode()) + len(json.dumps(schema).encode()) + 1024 <= 24576
    assert schema['properties']['topics']['maxItems'] == 8
    assert schema['properties']['topics']['items']['properties']['angle']['maxLength'] == 160


def supporting_plan(material, overlays=None, picture=True):
    source, evidence, ids = material
    sample = {'id': 'still', 'source_id': 's', 'at': 31., 'start': 30., 'end': 32.,
              'artifact_key': 'cached', 'media_identity': {'hash': 'abc'}}
    if picture:
        sample['picture'] = {'id': 'still', 'source': 's', 'at': 31., 'data': 'jpeg'}
    def invoke(stage, prompt, schema, tokens, **kwargs):
        return {'proposal': {'title': 'Water', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.',
            'selections': [{'unit_id': i} for i in ids[:2]],
            'supporting_pictures': overlays if overlays is not None else [{'sample_id': 'still', 'over_unit_id': ids[1]}]}}
    result = editor.plan_short(candidate(ids[:2]), [evidence], {'s': source}, {}, invoke, visual_samples=[sample])
    return result, sample


def test_supporting_exact_compilation_audio_and_review_proof(material):
    source, evidence, ids = material
    result, sample = supporting_plan(material)
    assert result['timeline']['clips'] == plan(material)['timeline']['clips']
    shot = result['timeline']['broll'][0]
    assert (shot['start'], shot['end'], shot['at']) == (30, 32, 8)
    assert result['visual_evidence'][0]['over_unit_id'] == ids[1]
    assert 'picture' not in result['visual_evidence'][0]
    seen = []
    editor.review_short(result, [evidence], {'s': source}, {}, invoke_for({'status': 'pass', 'issues': []}, seen))
    assert json.loads(seen[0][1].split('\n', 1)[1])['missing_supporting_frames'] == ['still']
    shot['start'] += .1
    with pytest.raises(ValueError, match='proof'):
        editor.review_short(result, [evidence], {'s': source}, {}, invoke_for({'status': 'pass', 'issues': []}))


def test_supporting_invalid_refs_optional_and_global_framing(material):
    source, evidence, ids = material
    for overlays in ([{'sample_id': 'invented', 'over_unit_id': ids[0]}],
                     [{'sample_id': 'still', 'over_unit_id': ids[-1]}]):
        with pytest.raises(ValueError, match='attached sample'):
            supporting_plan(material, overlays)
    with pytest.raises(ValueError, match='attached sample'):
        supporting_plan(material, picture=False)
    empty, _ = supporting_plan(material, [])
    assert empty['timeline']['broll'] == [] and empty['visual_evidence'] == []
    result, _ = supporting_plan(material)
    framing = {'mode': 'fill', 'zoom': 1.2, 'x': .4, 'y': .6}
    changed = editor.apply_settings(result, [evidence], {'s': source}, {'framing': framing})
    assert changed['timeline']['broll'][0]['framing'] == framing
    editor.review_short(changed, [evidence], {'s': source}, {}, invoke_for({'status': 'pass', 'issues': []}))
    with pytest.raises(ValueError, match='scope'):
        editor.review_short(result, [evidence], {'s': source}, {'source_roles': {'s': 'unknown'}}, invoke_for({}))
    result['visual_evidence'] = []
    with pytest.raises(ValueError, match='proof'):
        editor.review_short(result, [evidence], {'s': source}, {}, invoke_for({}))


def test_supporting_prompt_hides_local_cache_identity_but_preserves_proof(material):
    source, evidence, ids = material
    sample = {'id': 'private-still', 'source_id': 's', 'at': 31., 'start': 30., 'end': 32.,
              'artifact_key': '/private/cache/item', 'media_identity': {'path': '/private/source.mp4', 'hash': 'abc'},
              'picture': {'id': 'private-still', 'source': 's', 'at': 31., 'data': 'jpeg'}}
    prompts = []
    def invoke(stage, prompt, schema, tokens, **kwargs):
        prompts.append(prompt)
        if stage == 'review':
            return {'proposal': {'status': 'uncertain', 'issues': []}}
        return {'proposal': {'title': 'Water', 'form': 'moment', 'edit_format': 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': [{'unit_id': ids[0]}],
                             'supporting_pictures': [{'sample_id': sample['id'], 'over_unit_id': ids[0]}]}}
    result = editor.plan_short(candidate(ids[:1]), [evidence], {'s': source}, {}, invoke, visual_samples=[sample])
    assert result['visual_evidence'][0]['media_identity'] == sample['media_identity']
    assert result['visual_evidence'][0]['artifact_key'] == sample['artifact_key']
    editor.review_short(result, [evidence], {'s': source}, {}, invoke, visual_samples=[sample])
    assert all('/private/' not in prompt and 'media_identity' not in prompt and 'artifact_key' not in prompt for prompt in prompts)
    for field, value in [('artifact_key', 'changed'), ('media_identity', {'hash': 'tampered'}), ('at', 30.5)]:
        changed = {**sample, field: value, 'picture': None}
        with pytest.raises(ValueError, match='identity changed'):
            editor.review_short(result, [evidence], {'s': source}, {}, invoke, visual_samples=[changed])


def test_a_malformed_candidate_is_rejected_while_good_ones_survive(material):
    _, evidence, ids = material
    good = candidate(ids[:2])
    long_hook = {**candidate(ids[2:4]), 'id': 'b', 'hook_line': 'one two three four five six seven eight nine ten'}
    unknown = {**candidate(['not-a-unit']), 'id': 'c'}
    response = {'candidates': [good, long_hook, unknown, 'junk'], 'rejected': [{'id': 'x'}],
                'duplicate_groups': [['a', 'missing']], 'explanation': 'Mixed.'}
    result = editor.discover([{'topics': [{'angle': 'w', 'unit_ids': ids[:1]}]}], [evidence], {}, [], invoke_for(response))
    assert [c['id'] for c in result['candidates']] == ['a']
    reasons = {r['id']: r['reason'] for r in result['rejected']}
    assert 'nine words' in reasons['b'] and 'unit' in reasons['c'].lower() and 'invalid-4' in reasons
    assert result['duplicate_groups'] == []


def test_long_sources_discover_in_windows_and_rank_together(material, monkeypatch):
    _, evidence, ids = material
    monkeypatch.setattr(editor, 'DISCOVERY_WINDOW_CHARS', 200)
    calls = []

    def invoke(stage, prompt, schema, tokens, **kw):
        payload = json.loads(prompt.split('\n', 1)[1])
        calls.append(payload)
        window = [row[0] for row in payload['transcript']]
        found = {**candidate(window[:2]), 'id': 'a'} if len(window) >= 2 else None
        return {'proposal': {'candidates': [found] if found else [], 'rejected': [], 'duplicate_groups': [],
                             'explanation': 'part'}, 'usage': {'input_tokens': 10, 'output_tokens': 5}}
    result = editor.discover([{'topics': [{'angle': 'w', 'unit_ids': ids[:1]}]}], [evidence], {}, [], invoke)
    assert len(calls) > 1 and 'part 1 of' in calls[0]['task']
    covered = [row[1:] for c in calls for row in c['transcript']]
    assert covered == [row[1:] for row in editor._transcript(*editor._index([evidence])[:1], {})]
    assert len({c['id'] for c in result['pool']}) == len(result['pool'])
    assert all(c['id'].startswith('w') for c in result['pool'])
    assert result['model_usage']['input_tokens'] == 10 * len(calls)


def test_windows_see_earlier_ideas_and_near_duplicates_are_dropped(material, monkeypatch):
    _, evidence, ids = material
    monkeypatch.setattr(editor, 'DISCOVERY_WINDOW_CHARS', 200)
    calls = []

    def invoke(stage, prompt, schema, tokens, **kw):
        payload = json.loads(prompt.split('\n', 1)[1])
        calls.append(payload)
        window = [row[0] for row in payload['transcript']]
        if len(window) < 2:
            return {'proposal': {'candidates': [], 'rejected': [], 'duplicate_groups': [], 'explanation': 'none'}}
        # every window proposes the "same" idea in identical words, as the barium case did
        found = {**candidate(window[:2]), 'id': 'x', 'title': 'Water decides whether storage works',
                 'angle': 'Water decides whether storage works'}
        return {'proposal': {'candidates': [found], 'rejected': [], 'duplicate_groups': [], 'explanation': 'part'}}
    result = editor.discover([{'topics': [{'angle': 'w', 'unit_ids': ids[:1]}]}], [evidence], {}, [], invoke)
    assert len(calls) > 1 and 'already_proposed' not in calls[0] and calls[1]['already_proposed']
    assert len(result['candidates']) == 1
    assert any('Same idea as' in r['reason'] or 'duplicate' in r['reason'].lower() for r in result['rejected'])


def test_dense_short_segments_map_without_overflowing(tmp_path):
    # Like a fast talker transcribed in ~4-word Whisper segments: compact transport, larger envelope if needed.
    source = {'id': 'dense', 'duration': 2000, 'segments': [
        {'start': i * 2.0, 'end': i * 2.0 + 1.5, 'text': f'quick words number {i}'} for i in range(900)]}
    evidence = prepare_evidence(source, tmp_path)
    seen = []

    def invoke(stage, prompt, schema, tokens, **kw):
        seen.append((len(prompt), kw))
        return {'proposal': {'topics': []}, 'usage': {}}
    for packet in evidence['artifact']['packets']:
        editor.map_packet(packet, evidence, invoke)
    assert seen and all(size < 120_000 for size, _ in seen)


def test_review_asks_again_for_missing_clips_then_holds_only_those(material):
    source, evidence, ids = material
    items = [{'id': f'c{i}', 'title': 'Water decides whether it works', 'hook_line': 'Water decides whether it works',
              'unit_ids': ids[i:i + 1]} for i in range(3)]
    calls = []

    def invoke(stage, prompt, schema, tokens, **kw):
        asked = [c['id'] for c in json.loads(prompt.split('\n', 1)[1])['candidates']]
        calls.append(asked)
        first = [{'id': asked[0], 'status': 'pass', 'issues': []}, {'id': 'bogus', 'status': 'pass', 'issues': []}]
        return {'proposal': {'results': first if len(calls) == 1 else []}, 'usage': {'input_tokens': 5}}
    out = editor.review_batch(items, [evidence], {'s': source}, {}, invoke)
    assert calls == [['c0', 'c1', 'c2'], ['c1', 'c2']]
    assert set(out['results']) == {'c0', 'c1', 'c2'}
    assert 'Not reviewed' in out['results']['c2']['issues'][0]['detail']
