"""Compile a reviewed discovery candidate straight to timeline v3, with no model call.

Evidence unit IDs become segments through the existing word-span resolution; filler/silence cuts, caption
template, hook overlay, progress bar and framing come from project options with mainstream defaults.
A trimmed lead-in and an optional cold-open teaser come from discovery (docs/v3/GENERATION_V3.md, "Openings").
The v2 plan and timeline are produced too, so collection paths that still read them keep working.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

from . import collection_editor as editor, studio_engine as engine, studio_timeline as tl
from .hooks import HOOK_MAX_WORDS, check_hook  # noqa: F401  (re-exported for callers)
from .planner import validate_timeline
from .word_spans import resolve_selection

DEFAULT_TEMPLATE = 'bold_pop'
HOOK_STYLE = 'hook_box'
HOOK_SECONDS = 3.0
# The hook stays up until the sentence being spoken around three seconds ends, within these bounds.
HOOK_MIN_SECONDS, HOOK_MAX_SECONDS, HOOK_FALLBACK_SECONDS = 2.5, 5.0, 4.0
HOOK_HOLDS = ('sentence', 'full')
MERGE_GAP = 0.5
CANVAS = {'9:16': (1080, 1920), '16:9': (1920, 1080)}
DEFAULT_OPTIONS = {'template_id': None, 'brand_kit_id': None, 'remove_fillers': True, 'silence_min': 0.6,
                   'progress_bar': True, 'auto_reframe': True, 'hook_hold': 'sentence'}


def hook_end(words, duration, hold='sentence'):
    """When the hook leaves the screen: the end of the sentence heard around three seconds, or the whole clip."""
    if hold == 'full':
        return round(duration, 3)
    end = None
    for word, following in zip(words, list(words[1:]) + [None]):
        if word['end'] < HOOK_MIN_SECONDS:
            continue
        if word['end'] > HOOK_MAX_SECONDS:
            break
        pause = following is not None and following['start'] - word['end'] >= 0.3
        if re.search(r'[.!?]["\u201d\u2019)\]]*$', str(word['text']).strip()) or pause:
            end = max(HOOK_SECONDS, word['end'] + 0.15)
            break
    return round(min(end or HOOK_FALLBACK_SECONDS, HOOK_MAX_SECONDS, duration), 3)


def read_options(sources_root, collection_id):
    """Project options and brand kit saved by the studio, merged over mainstream defaults."""
    options = dict(DEFAULT_OPTIONS)
    kit = None
    path = Path(sources_root) / 'studio.sqlite3'
    if path.exists():
        try:
            from .studio_store import StudioStore
            store = StudioStore(sources_root)
            saved = store.project_options(collection_id)
            options.update({k: saved[k] for k in DEFAULT_OPTIONS if saved.get(k) is not None})
            if options['brand_kit_id']:
                try:
                    kit = store.get_kit(options['brand_kit_id'])
                except KeyError:
                    kit = None
        except (OSError, ValueError):
            pass
    return options, kit


def _segments(resolved):
    """One segment per unit; same-source neighbours separated by at most MERGE_GAP seconds merge."""
    merged = []
    for unit in resolved:
        last = merged[-1] if merged else None
        if last and last['source_id'] == unit['source_id'] and 0 <= unit['start'] - last['end'] <= MERGE_GAP:
            last['end'] = unit['end']
        else:
            merged.append({'source_id': unit['source_id'], 'start': unit['start'], 'end': unit['end'], 'cuts': []})
    for number, segment in enumerate(merged, 1):
        segment['id'] = f's{number}'
    return merged


def _canvas(aspect):
    width, height = CANVAS[aspect]
    return {'width': width, 'height': height, 'fps': 30, 'background': {'kind': 'blur', 'color': '#000000'}}


def _overlays(hook, duration, options, words=()):
    overlays = []
    if hook:
        hold = options.get('hook_hold') if options.get('hook_hold') in HOOK_HOLDS else 'sentence'
        overlays.append({'id': 'o1', 'kind': 'hook', 'text': hook, 'start': 0.0,
                         'end': hook_end(list(words), duration, hold), 'style': HOOK_STYLE})
    if options['progress_bar']:
        overlays.append({'id': 'o2', 'kind': 'progress_bar', 'color': '#FFD400', 'position': 'bottom', 'thickness': 12})
    return overlays


def _reframe(timeline, jobs, cache_dir):
    """Auto-reframe shots with a short reason, or None when the vision analyzers are unavailable."""
    try:
        shots = engine.auto_shots(timeline, jobs, cache_dir=cache_dir)
        engine.validate({**timeline, 'shots': shots}, jobs)
        return shots, 'auto'
    except engine.EngineUnavailable:
        return None, 'fit_vision_unavailable'
    except (ValueError, OSError, KeyError) as error:
        return None, 'fit_reframe_failed: ' + str(error)[:120]


def build_v3(resolved, jobs, aspect, hook, options, *, kit=None, cache_dir=None, detect=None, teaser=None):
    """Return `(timeline_v3, notes)` for resolved evidence units; `jobs` carry transcript words.

    `teaser` is an optional resolved cold-open phrase played before the clip as segment `cold`.
    """
    segments = _segments(resolved)
    if teaser:
        segments = [{'id': 'cold', 'source_id': teaser['source_id'], 'start': teaser['start'], 'end': teaser['end'],
                     'cuts': [], 'role': 'teaser'}] + segments
    base = {'schema_version': 3, 'canvas': _canvas(aspect), 'segments': segments, 'shots': [], 'broll': [],
            'captions': {'enabled': True, 'template_id': options['template_id'] or DEFAULT_TEMPLATE,
                         'overrides': {}, 'emphasis': [], 'hidden': []},
            'overlays': [], 'brand_kit_id': None, 'audio': {'normalize': True}}
    notes = {'cuts': 0, 'reframe': 'off', **({'cold_open': True} if teaser else {})}
    if options['remove_fillers']:
        try:
            base, _ = tl.add_filler_cuts(base, jobs, detect or engine.detect_fillers, options['silence_min'])
        except engine.EngineUnavailable:
            notes['fillers'] = 'unavailable'
    timeline = engine.validate(base, jobs)
    notes['cuts'] = sum(len(s['cuts']) for s in timeline['segments'])
    total = engine.duration(timeline)
    words = engine.program_words(timeline, jobs) if hook else []
    timeline = engine.validate({**timeline, 'overlays': _overlays(hook, total, options, words)}, jobs)
    if kit:
        timeline = engine.validate(tl.apply_brand_kit(timeline, kit), jobs)
    if options['auto_reframe']:
        shots, notes['reframe'] = _reframe(timeline, jobs, cache_dir)
        if shots:
            timeline = engine.validate({**timeline, 'shots': shots}, jobs)
    return timeline, notes


def resolve_proof(unit, artifact, span):
    """Canonical saved proof of a lead or teaser span (what `word_spans.resolve_plan` re-derives)."""
    return resolve_selection(unit, artifact, {k: span[k] for k in ('start_word', 'end_word', 'kind')})[1]


def _v2(resolved, constraints, jobs):
    framing = constraints.get('framing', {'mode': 'fit', 'zoom': 1, 'x': .5, 'y': .5})
    clips = [{'id': f'c{n}', 'source_id': unit['source_id'], 'start': unit['start'], 'end': unit['end'],
              'framing': constraints.get('shot_framing', {}).get(unit['id'], framing)}
             for n, unit in enumerate(resolved, 1)]
    timeline = {'schema_version': 2, 'clips': clips, 'broll': [], 'titles': [],
                'aspect_ratio': constraints.get('aspect_ratio', '9:16'), 'captions': constraints.get('captions', True),
                'canvas_style': constraints.get('canvas_style', 'plain')}
    return timeline


def compile_candidate(candidate, evidences, sources, constraints, options, *, kit=None, cache_dir=None, detect=None):
    """Compile one candidate into `{plan, timeline, timeline_v3, notes}`; raises ValueError when invalid."""
    artifacts, units = editor._index(evidences)
    ids = editor._refs(candidate.get('unit_ids'), units)
    editor._scope(ids, units, sources, constraints)
    lead, teaser = editor.effective_opening(candidate, constraints)
    if teaser:
        editor._scope([teaser['unit_id']], units, sources, constraints)
    spoken = editor.spoken_units(candidate, artifacts, units, constraints)
    teaser_unit, resolved = (spoken[0], spoken[1:]) if teaser else (None, spoken)
    jobs = {sid: {**sources[sid], **a['transcript']} for sid, a in artifacts.items() if sid in sources}
    hook = check_hook(candidate['hook_line']) if (candidate.get('hook_line') or '').strip() else ''
    aspect = constraints.get('aspect_ratio', '9:16')
    timeline, notes = build_v3(resolved, jobs, aspect, hook, options, kit=kit, cache_dir=cache_dir, detect=detect,
                               teaser=teaser_unit)
    style = constraints.get('caption_style', 'auto')
    style = 'clean_focus' if style == 'auto' else style
    choice = editor._caption_selection(style, 'Mainstream caption template from the project defaults.',
                                       constraints.get('caption_style', 'auto'))
    v2 = _v2(resolved, constraints, jobs)
    v2.update(caption_style=choice['style'])
    for key in ('width', 'height', 'fps'):
        if key in constraints:
            v2[key] = constraints[key]
    v2 = validate_timeline(v2, sources)
    spans = {}
    if lead:
        unit = units[lead['unit_id']]
        spans[lead['unit_id']] = resolve_proof(unit, artifacts[unit['source_id']], lead)
    plan = {'schema_version': 1, 'title': candidate['title'], 'angle': candidate['angle'], 'form': candidate['form'],
            'edit_format': candidate['edit_format'], 'format_reason': 'Chosen at discovery from the selected speech.',
            'caption_choice': choice, 'unit_ids': ids, 'timeline': v2, 'visual_evidence': [],
            'constraints': copy.deepcopy(constraints), 'evidence_provenance': editor._provenance(artifacts, ids),
            'model_usage': {}, 'compiled_by': 'v3_compiler', 'hook_line': hook,
            'post_title': candidate.get('post_title') or candidate['title'],
            'post_description': candidate.get('post_description') or '',
            'hashtags': list(candidate.get('hashtags') or []),
            **({'payoff_unit_id': candidate['payoff_unit_id']} if candidate.get('payoff_unit_id') else {}),
            **({'word_spans': spans} if spans else {}),
            **({'cold_open': {'unit_id': teaser['unit_id'],
                              **resolve_proof(units[teaser['unit_id']], artifacts[units[teaser['unit_id']]['source_id']], teaser)}}
               if teaser else {})}
    return {'plan': plan, 'timeline': v2, 'timeline_v3': timeline, 'notes': notes}
