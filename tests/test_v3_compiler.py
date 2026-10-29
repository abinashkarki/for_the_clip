"""Candidates compile to timeline v3 locally: cuts, hook, progress bar, template and framing defaults."""
import copy

import pytest

from clippie import studio_engine as engine, v3_compiler as compiler
from clippie.analyzers import AnalyzerUnavailable
from clippie.source_evidence import prepare_evidence
from clippie.word_spans import resolve_plan


def words_for(sentences, step=.35, gaps=None):
    """Words at a steady pace; `gaps` maps a word index to extra silence before it."""
    words, at = [], 0.
    for text in sentences:
        for token in text.split():
            at += (gaps or {}).get(len(words), 0)
            words.append({'id': len(words), 'text': token, 'start': round(at, 3), 'end': round(at + .3, 3)})
            at += step
    return words


@pytest.fixture
def material(tmp_path):
    words = words_for(['So um the water flows downhill steadily.', 'Storage shifts supply into the evening.'],
                      gaps={5: 1.0, 7: 1.4})
    source = {'id': 's', 'duration': 30., 'words': words, 'source_path': str(tmp_path / 'missing.mp4')}
    evidence = prepare_evidence(source, tmp_path)
    ids = [u['id'] for u in evidence['artifact']['units']]
    candidate = {'id': 'c1', 'title': 'Water and storage', 'angle': 'Water flows', 'form': 'moment',
                 'edit_format': 'explanation', 'hook_line': 'Water flows downhill', 'unit_ids': ids}
    return source, evidence, candidate


def compile_(material, options=None, constraints=None, **kwargs):
    source, evidence, candidate = material
    return compiler.compile_candidate(candidate, [evidence], {'s': source}, constraints or {},
                                      {**compiler.DEFAULT_OPTIONS, **(options or {})}, **kwargs)


@pytest.fixture(autouse=True)
def no_vision(monkeypatch):
    def unavailable(*args, **kwargs):
        raise AnalyzerUnavailable('face model missing')
    monkeypatch.setattr('clippie.analyzers.faces.track', unavailable)


def test_defaults_template_hook_progress_cuts_and_fit_fallback(material):
    result = compile_(material)
    timeline = result['timeline_v3']
    assert timeline['schema_version'] == 3 and timeline['captions']['template_id'] == 'bold_pop'
    assert timeline['canvas']['width'] == 1080 and timeline['canvas']['background']['kind'] == 'blur'
    hook, bar = timeline['overlays']
    assert (hook['kind'], hook['style'], hook['text'], hook['start']) == ('hook', 'hook_box', 'Water flows downhill', 0.0)
    # The hook stays until the sentence heard around three seconds ends, not a fixed three seconds.
    words = engine.program_words(timeline, {'s': material[0]})
    assert hook['end'] == compiler.hook_end(words, engine.duration(timeline))
    assert compiler.HOOK_MIN_SECONDS <= hook['end'] <= compiler.HOOK_MAX_SECONDS
    assert (bar['kind'], bar['position']) == ('progress_bar', 'bottom')
    reasons = {cut['reason'] for segment in timeline['segments'] for cut in segment['cuts']}
    assert reasons == {'filler', 'silence'} and result['notes']['cuts'] >= 2
    assert [(s['layout'], s['start']) for s in timeline['shots']] == [('fit', 0.0)]  # no vision: whole frame on blur
    assert result['notes']['reframe'] == 'fit_vision_unavailable'
    assert engine.validate(timeline, {'s': material[0]}) == timeline


def test_silence_and_fillers_can_be_disabled(material):
    timeline = compile_(material, {'remove_fillers': False})['timeline_v3']
    assert all(segment['cuts'] == [] for segment in timeline['segments'])


def test_cuts_shorten_the_program_but_never_remove_content_words(material):
    source = material[0]
    full = compile_(material, {'remove_fillers': False})['timeline_v3']
    cut = compile_(material)['timeline_v3']
    assert engine.duration(cut) < engine.duration(full)
    spoken = [w['text'] for w in engine.program_words(cut, {'s': source})]
    assert 'um' not in spoken and {'water', 'flows', 'downhill', 'Storage', 'shifts'} <= set(spoken)


def test_template_option_and_brand_kit(material):
    assert compile_(material, {'template_id': 'karaoke'})['timeline_v3']['captions']['template_id'] == 'karaoke'
    kit = {'id': 'kit1', 'template_id': 'boxed_word', 'overrides': {}, 'hook_style': 'hook_outline',
           'progress_bar': {'enabled': False, 'color': '#112233'}, 'logo_asset_id': None}
    timeline = compile_(material, {'brand_kit_id': 'kit1'}, kit=kit)['timeline_v3']
    assert timeline['captions']['template_id'] == 'boxed_word' and timeline['brand_kit_id'] == 'kit1'
    assert [o['kind'] for o in timeline['overlays']] == ['hook'] and timeline['overlays'][0]['style'] == 'hook_outline'


def test_reframe_shots_are_used_when_vision_is_available(material, monkeypatch):
    def shots(timeline, sources, **kwargs):
        total = engine.duration(timeline)
        return [{'id': 'sh1', 'start': 0.0, 'end': total, 'layout': 'fill',
                 'regions': [{'keyframes': [{'t': 0.0, 'x': .6, 'y': .4, 'zoom': 1.0}]}]}]
    monkeypatch.setattr(engine, 'auto_shots', shots)
    result = compile_(material)
    assert result['timeline_v3']['shots'][0]['layout'] == 'fill' and result['notes']['reframe'] == 'auto'
    monkeypatch.setattr(engine, 'auto_shots', lambda *a, **k: [{'id': 'bad'}])
    assert compile_(material)['notes']['reframe'].startswith('fit_reframe_failed')
    monkeypatch.setattr(engine, 'auto_shots', lambda *a, **k: pytest.fail('reframe was disabled'))
    assert compile_(material, {'auto_reframe': False})['notes']['reframe'] == 'off'


def test_v2_plan_stays_exactly_resolvable_so_older_paths_keep_working(material):
    _, evidence, _ = material
    result = compile_(material)
    plan = result['plan']
    assert [u['id'] for u in resolve_plan(plan, [evidence])] == plan['unit_ids']
    assert plan['timeline']['schema_version'] == 2 and plan['caption_choice']['style'] == 'clean_focus'
    assert plan['edit_format'] == 'explanation' and plan['hook_line'] == 'Water flows downhill'


def test_far_apart_units_stay_separate_segments_and_adjacent_ones_merge(material):
    source, evidence, candidate = material
    units = evidence['artifact']['units']
    assert len(units) == 2
    assert units[1]['start'] - units[0]['end'] > compiler.MERGE_GAP
    timeline = compile_(material, {'remove_fillers': False})['timeline_v3']
    assert len(timeline['segments']) == 2  # a 1.4 s pause separates the sentences
    close = [{**units[0]}, {**units[1], 'start': units[0]['end'] + .2}]
    assert [s['end'] for s in compiler._segments(close)] == [close[1]['end']]  # a 0.2 s gap merges


@pytest.mark.parametrize('hook', ['', ' ', 'x ' * 12, None, 'y' * 121])
def test_invalid_hooks_are_rejected(material, hook):
    source, evidence, candidate = material
    candidate = {**copy.deepcopy(candidate), 'hook_line': hook}
    if hook in ('', ' ', None):
        # An empty hook means no overlay, never invented text.
        result = compiler.compile_candidate(candidate, [evidence], {'s': source}, {}, dict(compiler.DEFAULT_OPTIONS))
        assert all(o['kind'] != 'hook' for o in result['timeline_v3']['overlays'])
    else:
        with pytest.raises(ValueError):
            compiler.compile_candidate(candidate, [evidence], {'s': source}, {}, dict(compiler.DEFAULT_OPTIONS))


def test_hooks_are_grounded_by_numbers_and_names_not_word_overlap():
    # Word overlap no longer gates a hook (docs/v3/GENERATION_V3.md, "Hooks"): a curiosity hook may use new words.
    from clippie import hooks
    speech = 'The water flows downhill. Storage shifts supply into the evening.'
    assert hooks.ungrounded('Where does the evening power come from?', speech) == []
    assert hooks.ungrounded('Storage cuts bills by 40 percent', speech) == ['40']
    assert compiler.check_hook('Water flows downhill') == 'Water flows downhill'


def test_read_options_defaults_without_a_studio_store(tmp_path):
    options, kit = compiler.read_options(tmp_path, 'c')
    assert options == compiler.DEFAULT_OPTIONS and kit is None
    assert not (tmp_path / 'studio.sqlite3').exists()  # reading must not create a studio database
