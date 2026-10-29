"""Openings, hooks and public copy for shorts: lead-in trims, cold opens, the blind opening judge, curiosity hooks
that stay grounded, and the post title, caption and hashtags. Synthetic mechanics only, not editorial quality."""
import json

import pytest

from clippie import collection_editor as editor, hooks, post_copy, v3_compiler as compiler
from clippie.analyzers import AnalyzerUnavailable
from clippie.source_evidence import prepare_evidence
from clippie.word_spans import lead_span, teaser_span, LEAD_MAX_DROPPED

SENTENCES = ['And then also, this is starting to feel off topic, but math is an oral culture.',
             'Nobody actually learns it from the papers.',
             'They learn it by talking to each other at the board.',
             'That is why the seminar still matters so much.']


def words_for(sentences, step=.35):
    words, at = [], 0.
    for text in sentences:
        for token in text.split():
            words.append({'id': len(words), 'text': token, 'start': round(at, 3), 'end': round(at + .3, 3)})
            at += step
        at += .5
    return words


@pytest.fixture
def material(tmp_path):
    source = {'id': 's', 'duration': 60., 'words': words_for(SENTENCES), 'source_path': str(tmp_path / 'missing.mp4')}
    evidence = prepare_evidence(source, tmp_path)
    units = evidence['artifact']['units']
    return source, evidence, units


@pytest.fixture(autouse=True)
def no_vision(monkeypatch):
    def unavailable(*args, **kwargs):
        raise AnalyzerUnavailable('face model missing')
    monkeypatch.setattr('clippie.analyzers.faces.track', unavailable)


def judged(**scores):
    return {name: {'score': scores.get(name, 4), 'evidence': f'{name} evidence'}
            for name in ('hook', 'standalone', 'payoff', 'density', 'shareability')}


# -- hook rules -----------------------------------------------------------------------------------------------

def test_a_curiosity_hook_need_not_reuse_the_speech_words():
    speech = ' '.join(SENTENCES)
    assert hooks.problems('Why do mathematicians still need a chalkboard?', speech, SENTENCES[0]) == []


def test_a_hook_that_repeats_the_opening_words_fails():
    opening = 'This is a 2 .4 -liter diesel. It is a new generation of the engine'
    assert hooks.echoes_opening('This is a 2.4-liter diesel engine', opening)
    assert any('repeats the opening' in p for p in hooks.problems('This is a 2.4-liter diesel engine', opening, opening))
    assert not hooks.echoes_opening('What makes this diesel worth the wait?', opening)


def test_numbers_and_names_in_a_hook_must_be_spoken():
    speech = 'So it starts here in Australia at just under $70 ,000, which is a big old chunk of change.'
    assert hooks.ungrounded('Why does it cost $70,000 here?', speech) == []    # ASR digit spacing still matches
    assert hooks.ungrounded('Why does it cost $80,000 here?', speech) == ['80000']
    assert hooks.ungrounded('Is Australia paying too much?', speech) == []
    assert hooks.ungrounded('Is Toyota paying too much?', speech) == ['Toyota']
    assert hooks.ungrounded('Why Australia Pays More Than Everyone', speech) == []   # Title Case is not names


def test_hook_word_limit():
    with pytest.raises(ValueError, match='at most 9 words'):
        hooks.check_hook('one two three four five six seven eight nine ten')


# -- lead trims and teasers -----------------------------------------------------------------------------------

def test_a_long_lead_in_is_trimmed_to_where_the_point_starts(material):
    _, evidence, units = material
    row = lead_span(units[0], evidence['artifact'], 'math is an oral culture.')
    assert row == {'start_word': 11, 'end_word': 15, 'kind': 'lead'}   # 11 of 16 words dropped: still allowed


def test_a_lead_trim_never_drops_a_negation_or_leaves_too_little(tmp_path):
    source = {'id': 's', 'duration': 30., 'source_path': str(tmp_path / 'm.mp4'),
              'words': words_for(['I do not think the market is ready for it yet.'])}
    evidence = prepare_evidence(source, tmp_path)
    unit = evidence['artifact']['units'][0]
    with pytest.raises(ValueError, match='negation'):
        lead_span(unit, evidence['artifact'], 'the market is ready for it yet.')
    with pytest.raises(ValueError, match='keeps at least'):
        lead_span(unit, evidence['artifact'], 'it yet.')
    with pytest.raises(ValueError, match='verbatim'):
        lead_span(unit, evidence['artifact'], 'the market is booming')
    assert lead_span(unit, evidence['artifact'], 'I do not think') is None   # already opens there


def test_lead_trim_length_is_bounded(tmp_path):
    long_lead = ' '.join(['well'] * (LEAD_MAX_DROPPED + 1)) + ' storage shifts supply into the evening.'
    source = {'id': 's', 'duration': 60., 'source_path': str(tmp_path / 'm.mp4'), 'words': words_for([long_lead])}
    evidence = prepare_evidence(source, tmp_path)
    with pytest.raises(ValueError, match='at most'):
        lead_span(evidence['artifact']['units'][0], evidence['artifact'], 'storage shifts supply into the evening.')


def test_teaser_is_a_short_verbatim_phrase(material):
    _, evidence, units = material
    assert teaser_span(units[2], evidence['artifact'], 'by talking to each other') == \
        {'start_word': 3, 'end_word': 7, 'kind': 'teaser'}
    with pytest.raises(ValueError, match='verbatim'):
        teaser_span(units[2], evidence['artifact'], 'by shouting at each other')
    with pytest.raises(ValueError, match='at least'):
        teaser_span(units[2], evidence['artifact'], 'the board.')


# -- hook timing ----------------------------------------------------------------------------------------------

def test_hook_holds_until_the_sentence_heard_near_three_seconds_ends():
    words = [{'text': w, 'start': i * .4, 'end': i * .4 + .3} for i, w in enumerate('a b c d e f g h end. next'.split())]
    assert compiler.hook_end(words, 20.) == round(words[8]['end'] + .15, 3)
    unpunctuated = [{'text': 'w', 'start': i * .2, 'end': i * .2 + .2} for i in range(60)]
    assert compiler.hook_end(unpunctuated, 20.) == compiler.HOOK_FALLBACK_SECONDS
    assert compiler.hook_end(words, 20., 'full') == 20.
    assert compiler.hook_end(words, 2.) == 2.


# -- public copy ----------------------------------------------------------------------------------------------

def test_hashtags_are_cleaned_grounded_and_bounded():
    speech = 'The 2026 Pajero is here.'
    tags = ['#Pajero', 'viral', 'FYP', 'offroad', 'pajero', 'pajero2026', 'pajero2027', '4x4', 'x', '12345',
            'suv', 'cars', 'mitsubishi']
    assert post_copy.clean_hashtags(tags, speech) == ['pajero', 'offroad', 'pajero2026', 'suv', 'cars']
    assert post_copy.clean_hashtags('not a list') == []


def test_caption_moves_inline_hashtags_to_tags():
    copy = post_copy.public_copy({'title': 'Label', 'post_title': '  Why seminars   still matter ',
                                  'post_description': 'A mathematician on how ideas spread. #math #Seminar',
                                  'hashtags': ['mathematics']}, '')
    assert copy == {'post_title': 'Why seminars still matter', 'post_description': 'A mathematician on how ideas spread.',
                    'hashtags': ['mathematics', 'math', 'seminar']}
    assert post_copy.public_copy({'title': 'Label', 'hashtags': 'math ai'})['hashtags'] == []
    assert post_copy.public_copy({'title': 'Label'})['post_title'] == 'Label'


# -- blind opening judge --------------------------------------------------------------------------------------

def slate(units, count):
    return [{'id': f'c{n}', 'hook_line': 'Why does this matter?', 'unit_ids': [units[n % 4]['id']]} for n in range(count)]


@pytest.mark.opening_judge
def test_opening_judge_sees_only_the_opening_and_caps_top_scores(material):
    _, evidence, units = material
    artifacts, index = editor._index([evidence])
    seen = []

    def invoke(stage, prompt, schema, tokens, **kwargs):
        seen.append(json.loads(prompt.split('\n', 1)[1]))
        rows = [{'id': f'c{n}', 'rank': n + 1, 'score': 5, 'evidence': 'grabs'} for n in range(8)]
        return {'proposal': {'openings': rows}}
    result, _, note = editor.judge_openings(slate(units, 8), artifacts, index, invoke)
    assert [result[f'c{n}']['score'] for n in range(8)] == [5, 5, 4, 4, 4, 4, 4, 4] and note == ''
    sent = seen[0]['candidates'][0]
    assert set(sent) == {'id', 'on_screen_hook', 'first_words_heard'}
    assert 'seminar' not in json.dumps(seen[0]['candidates'][0])          # c0 opening only, nothing later


@pytest.mark.opening_judge
def test_opening_judge_failure_falls_back_with_a_note(material):
    _, evidence, units = material
    artifacts, index = editor._index([evidence])
    result, _, note = editor.judge_openings(slate(units, 2), artifacts, index,
                                            lambda *a, **k: {'proposal': {'openings': [{'id': 'zz'}]}})
    assert result == {} and 'own scores' in note


# -- discovery to compile, end to end -------------------------------------------------------------------------

def discovery_invoke(candidate, openings_score=2, review=None, repairs=None):
    def invoke(stage, prompt, schema, tokens, **kwargs):
        body = json.loads(prompt.split('\n', 1)[1])
        if stage == 'discover':
            return {'proposal': {'candidates': [candidate], 'rejected': [], 'duplicate_groups': [], 'explanation': 'One.'}}
        if stage == 'review' and 'openings' in schema['properties']:
            return {'proposal': {'openings': [{'id': c['id'], 'rank': 1, 'score': openings_score, 'evidence': 'soft start'}
                                              for c in body['candidates']]}}
        if stage == 'review':
            return {'proposal': {'results': review or [{'id': c['id'], 'status': 'pass', 'issues': []}
                                                       for c in body['candidates']]}}
        if stage == 'repair':
            return {'proposal': {'repairs': repairs}}
        raise AssertionError(stage)
    return invoke


def proposed(**extra):
    return {'id': 'a', 'title': 'Math as oral culture', 'angle': 'Math spreads by talk', 'form': 'moment',
            'edit_format': 'explanation', 'hook_line': 'Why do mathematicians still need a chalkboard?',
            'unit_ids': ['e1_0', 'e1_1', 'e1_2', 'e1_3'], 'payoff_unit_id': 'e1_3',
            'open_from': 'math is an oral culture.', 'teaser': {'unit_id': 'e1_2', 'text': 'by talking to each other'},
            'post_title': 'The real way mathematicians learn', 'hashtags': ['Math', '#viral', 'mathematics', 'math2026'],
            'post_description': 'A mathematician on how ideas really spread. #math', 'components': judged(hook=5), **extra}


@pytest.mark.opening_judge
def test_discovery_resolves_openings_public_copy_and_blind_hook_score(material):
    source, evidence, units = material
    result = editor.discover([], [evidence], {}, [], discovery_invoke(proposed()))
    [chosen] = result['candidates']
    assert chosen['lead_trim'] == {'unit_id': units[0]['id'], 'start_word': 11, 'end_word': 15, 'kind': 'lead'}
    assert chosen['cold_open']['unit_id'] == units[2]['id'] and chosen['payoff_unit_id'] == units[3]['id']
    assert chosen['hashtags'] == ['math', 'mathematics']           # spam and an unspoken year dropped, dupes merged
    assert chosen['post_description'] == 'A mathematician on how ideas really spread.'
    assert chosen['hook_judge'] == 'blind_opening' and chosen['components']['hook']['score'] == 2
    assert chosen['self_hook']['score'] == 5
    assert chosen['duration'] < units[-1]['end'] - units[0]['start']   # the lead-in is no longer counted


def test_cold_open_can_be_switched_off(material):
    _, evidence, _ = material
    [chosen] = editor.discover([], [evidence], {'cold_open': False}, [], discovery_invoke(proposed()))['candidates']
    assert chosen['cold_open'] is None


def test_teaser_from_the_first_unit_is_ignored_with_a_note(material):
    _, evidence, _ = material
    candidate = proposed(teaser={'unit_id': 'e1_0', 'text': 'math is an oral culture.'})
    [chosen] = editor.discover([], [evidence], {}, [], discovery_invoke(candidate))['candidates']
    assert chosen['cold_open'] is None and any('later line' in n for n in chosen['opening_notes'])


def test_compile_plays_teaser_first_and_saves_copy_and_spans(material):
    source, evidence, units = material
    [chosen] = editor.discover([], [evidence], {}, [], discovery_invoke(proposed()))['candidates']
    compiled = compiler.compile_candidate(chosen, [evidence], {'s': source}, {}, dict(compiler.DEFAULT_OPTIONS))
    segments = compiled['timeline_v3']['segments']
    assert segments[0]['role'] == 'teaser' and segments[0]['end'] - segments[0]['start'] < 4.5
    assert segments[1]['start'] > units[0]['start']                  # the clip proper starts after the lead-in
    plan = compiled['plan']
    assert plan['hashtags'] == ['math', 'mathematics'] and plan['post_title'] == 'The real way mathematicians learn'
    assert plan['word_spans'][units[0]['id']]['kind'] == 'lead' and plan['cold_open']['kind'] == 'teaser'
    hook = next(o for o in compiled['timeline_v3']['overlays'] if o['kind'] == 'hook')
    assert compiler.HOOK_MIN_SECONDS <= hook['end'] <= compiler.HOOK_MAX_SECONDS


def test_review_fails_an_echo_hook_locally_and_repair_cleans_copy(material):
    source, evidence, units = material
    [chosen] = editor.discover([], [evidence], {'cold_open': False}, [],
                               discovery_invoke(proposed(hook_line='Math is an oral culture')))['candidates']
    review = editor.review_batch([chosen], [evidence], {'s': source}, {'cold_open': False}, discovery_invoke(chosen))
    issues = [i['detail'] for i in review['results']['a']['issues']]
    assert review['results']['a']['status'] == 'needs_revision' and any('repeats the opening' in i for i in issues)
    failed = [{**chosen, 'issues': review['results']['a']['issues']}]
    fixed = editor.repair_batch(failed, [evidence], {'s': source}, {'cold_open': False}, discovery_invoke(
        chosen, repairs=[{'id': 'a', 'title': 'Math as oral culture', 'hook_line': 'Where do mathematicians really learn?',
                          'post_title': 'Papers are not how math spreads', 'post_description': 'On seminars. #trending',
                          'hashtags': ['seminar', 'fyp']}]))
    assert fixed['repairs']['a']['hashtags'] == ['seminar']
    assert fixed['repairs']['a']['post_description'] == 'On seminars.'
    echo = editor.repair_batch(failed, [evidence], {'s': source}, {'cold_open': False}, discovery_invoke(
        chosen, repairs=[{'id': 'a', 'title': 'T', 'hook_line': 'Math is an oral culture', 'post_title': 'P',
                          'post_description': ''}]))
    assert echo['unrepaired'] == ['a']            # a repair that still echoes the opening is not accepted


def test_clip_summary_offers_generated_copy_with_metadata_fallback():
    from clippie.studio_api import clip_post_copy
    short = {'metadata': {'post_title': 'Old title', 'hashtags': ['math'], 'hook_line': 'Why?', 'post_description': 'Ctx.'}}
    assert clip_post_copy(short) == {'title': 'Old title', 'description': 'Ctx.', 'hashtags': ['math'], 'hook_line': 'Why?'}
    plan = {'post_title': 'Repaired title', 'hashtags': ['seminar'], 'hook_line': 'Where?'}
    assert clip_post_copy(short, plan) == {'title': 'Repaired title', 'description': 'Ctx.', 'hashtags': ['seminar'],
                                           'hook_line': 'Where?'}
    assert clip_post_copy({}) == {'title': '', 'description': '', 'hashtags': [], 'hook_line': ''}
