"""Pure timeline helpers for the Studio API: revision documents, words, brand kits, filler cuts.

Nothing here touches storage or the engine modules, so every rule can be tested directly.
All v3 shapes follow docs/v3/TIMELINE_V3.md.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone

from .studio_engine import kept_ranges

MIN_CUT = 0.04
EDGE = 0.02
MIN_SEGMENT = 0.2


def iso(stamp):
    return datetime.fromtimestamp(float(stamp), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def v2_digest(document):
    """Fingerprint of the v2 plan a v3 timeline was derived from."""
    payload = json.dumps(document.get('timeline'), sort_keys=True, allow_nan=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def v2_duration(document):
    clips = (document.get('timeline') or {}).get('clips') or []
    return round(sum(float(clip['end']) - float(clip['start']) for clip in clips), 3)


def stored_v3(document):
    """The studio timeline when it still matches the v2 plan it came from, else None."""
    stored = document.get('timeline_v3')
    if isinstance(stored, dict) and document.get('v2_digest') == v2_digest(document):
        return stored
    return None


def revision_duration(document):
    stored = stored_v3(document)
    if stored:
        from .studio_engine import duration
        return duration(stored)
    return v2_duration(document)


def first_source_time(document):
    """(source_id, seconds) of the clip's first frame, for thumbnails."""
    stored = stored_v3(document)
    if stored:
        segment = stored['segments'][0]
        return segment['source_id'], segment['start']
    clip = ((document.get('timeline') or {}).get('clips') or [None])[0]
    return (clip['source_id'], float(clip['start'])) if clip else (None, 0.0)


def revision_document(base, timeline, action):
    """Copy of the base document plus `timeline_v3`; stored v2 plan, review and gates stay as they were."""
    document = copy.deepcopy(base)
    document.update(timeline_v3=timeline, v2_digest=v2_digest(base), origin='studio_edit',
                    studio_action=action, human_review='unreviewed')
    editorial = document.setdefault('editorial', {})
    editorial['carried_review'] = {
        'scope': 'Studio edits are user-authored and have not received model review; '
                 'the review above covers the generated edit only.'}
    return document


def words_payload(timeline, sources, kept):
    """Every source word of every segment; cut words carry `removed: true` and null program times."""
    by_key = {word['key']: word for word in kept}
    emphasis = set(timeline.get('captions', {}).get('emphasis') or [])
    hidden = set(timeline.get('captions', {}).get('hidden') or [])
    result = []
    for segment in timeline['segments']:
        if segment.get('role') == 'teaser':
            continue  # its words also play later; the transcript lists each source word once
        for word in sources[segment['source_id']].get('words') or []:
            if not segment['start'] <= (word['start'] + word['end']) / 2 < segment['end']:
                continue
            key = f"{segment['source_id']}:{word['id']}"
            hit = by_key.get(key)
            result.append({
                'key': key, 'source_id': segment['source_id'], 'word_id': word['id'], 'text': word['text'],
                'start': round(word['start'], 3), 'end': round(word['end'], 3),
                'program_start': round(hit['start'], 3) if hit else None,
                'program_end': round(hit['end'], 3) if hit else None,
                'removed': hit is None,
                'emphasis': bool(hit['emphasis']) if hit else key in emphasis,
                'hidden': bool(hit['hidden']) if hit else key in hidden})
    return result


def _unique(items, prefix):
    taken = {item['id'] for item in items}
    name, number = prefix, 1
    while name in taken:
        number += 1
        name = f'{prefix}{number}'
    return name


def apply_brand_kit(timeline, kit):
    """Write the kit's template, overrides, hook style, progress bar and logo into a copy."""
    result = copy.deepcopy(timeline)
    captions = result.setdefault('captions', {})
    captions.update(template_id=kit['template_id'], overrides=copy.deepcopy(kit.get('overrides') or {}))
    overlays = result.setdefault('overlays', [])
    if kit.get('hook_style'):
        for overlay in overlays:
            if overlay['kind'] == 'hook':
                overlay['style'] = kit['hook_style']
    old = {kind: next((o for o in overlays if o['kind'] == kind), None) for kind in ('progress_bar', 'logo')}
    overlays[:] = [o for o in overlays if o['kind'] not in old]
    bar = kit.get('progress_bar') or {}
    if bar.get('enabled'):
        previous = old['progress_bar'] or {}
        overlays.append({'id': previous.get('id') or _unique(overlays, 'o-progress'), 'kind': 'progress_bar',
                         'color': bar['color'], 'position': previous.get('position', 'bottom'),
                         'thickness': previous.get('thickness', 12)})
    if kit.get('logo_asset_id'):
        previous = old['logo'] or {}
        overlays.append({'id': previous.get('id') or _unique(overlays, 'o-logo'), 'kind': 'logo',
                         'asset_id': kit['logo_asset_id'], 'anchor': previous.get('anchor', 'top_right'),
                         'scale': previous.get('scale', 0.14), 'opacity': previous.get('opacity', 0.9)})
    result['brand_kit_id'] = kit['id']
    return result


# -- filler cuts -----------------------------------------------------------

def _word_in(segment, word):
    return segment['start'] <= (word['start'] + word['end']) / 2 < segment['end']


def merge_cuts(segment, found):
    """Union existing and detected cuts. Cuts touching a segment edge trim the segment instead.

    Returns (start, end, cuts), or None when nothing usable would remain.
    """
    cuts = sorted(({'start': c['start'], 'end': c['end'], 'reason': c['reason']}
                   for c in [*(segment.get('cuts') or []), *found]), key=lambda c: (c['start'], c['end']))
    merged = []
    for cut in cuts:
        if merged and cut['start'] <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], cut['end'])
        else:
            merged.append(dict(cut))
    start, end = segment['start'], segment['end']
    while merged and merged[0]['start'] <= start + EDGE:
        start = max(start, merged.pop(0)['end'])
    while merged and merged[-1]['end'] >= end - EDGE:
        end = min(end, merged.pop()['start'])
    merged = [{**c, 'start': round(c['start'], 3), 'end': round(c['end'], 3)} for c in merged
              if c['end'] - c['start'] >= MIN_CUT and start < c['start'] and c['end'] < end]
    if end - start < MIN_SEGMENT:
        return None
    return round(start, 3), round(end, 3), merged


def _language(source):
    language = (source.get('provenance') or {}).get('language')
    return language if isinstance(language, str) and language else 'en'


def add_filler_cuts(timeline, sources, detect, silence_min):
    """(new timeline, changed). `detect` is `fillers.detect`; its 'repeat' reason is stored as 'filler'."""
    result = copy.deepcopy(timeline)
    for segment in result['segments']:
        source = sources[segment['source_id']]
        words = [word for word in source.get('words') or [] if _word_in(segment, word)]
        found = [{'start': item['start'], 'end': item['end'],
                  'reason': 'silence' if item['reason'] == 'silence' else 'filler'}
                 for item in detect(words, language=_language(source), silence_min=silence_min)]
        merged = merge_cuts(segment, found) if found else None
        if merged:
            segment['start'], segment['end'], segment['cuts'] = merged
    return result, result['segments'] != timeline['segments']


# -- program time ----------------------------------------------------------

def program_ranges(timeline):
    """[(source_id, src_start, src_end, at)] in program order (spec: program_map)."""
    return [r[:4] for r in _segment_ranges(timeline)]


def _segment_ranges(timeline):
    """Program ranges that also name their segment, so a cold-open teaser and the same words later stay apart."""
    ranges, at = [], 0.0
    for segment in timeline['segments']:
        for start, end in kept_ranges(segment):
            ranges.append((segment['source_id'], start, end, at, segment['id']))
            at += end - start
    return ranges


def _to_source(ranges, program):
    for source_id, start, end, at, *rest in ranges:
        if program <= at + (end - start) + 1e-9:
            return source_id, start + max(0.0, program - at), (rest or [None])[0]
    last = ranges[-1]
    return last[0], last[2], (last[4:] or [None])[0]


def _to_program(ranges, source_id, seconds, segment_id=None):
    if segment_id is not None:
        own = [r for r in ranges if r[0] == source_id and r[4:] and r[4] == segment_id
               and r[1] - 1e-9 <= seconds <= r[2] + 1e-9]
        if own:
            return own[0][3] + seconds - own[0][1]
    same = [r for r in ranges if r[0] == source_id]
    for _, start, end, at, *_ in same:
        if start - 1e-9 <= seconds <= end + 1e-9:
            return at + seconds - start
    later = [r for r in same if r[1] > seconds]
    if later:
        return min(later, key=lambda r: r[1])[3]
    return same[-1][3] + same[-1][2] - same[-1][1] if same else 0.0


def retimer(old, new):
    """Function mapping program seconds of `old` onto `new` (same content, different cuts)."""
    before, after = _segment_ranges(old), _segment_ranges(new)

    def convert(program):
        source_id, seconds, segment_id = _to_source(before, program)
        return round(_to_program(after, source_id, seconds, segment_id), 3)
    return convert


def _retime_shots(shots, convert, total):
    result = []
    for shot in shots:
        start, end = convert(shot['start']), convert(shot['end'])
        if end - start < 0.05:
            continue
        item = copy.deepcopy(shot)
        item.update(start=start, end=end)
        for region in item.get('regions') or []:
            frames, last = [], None
            for frame in region['keyframes']:
                moved = min(max(convert(frame['t']), start), end)
                if last is None or moved > last:
                    frames.append({**frame, 't': moved})
                    last = moved
            region['keyframes'] = frames
        result.append(item)
    if result:
        result[0]['start'] = 0.0
        result[-1]['end'] = total
        for before, after in zip(result, result[1:]):
            after['start'] = before['end']
    return result


def retime_program(old, new, total):
    """Carry shots, B-roll and hooks across a change of cuts so the result validates."""
    convert = retimer(old, new)
    result = copy.deepcopy(new)
    result['shots'] = _retime_shots(old.get('shots') or [], convert, total)
    result['broll'] = [item for item in (
        {**b, 'at': convert(b['at'])} for b in old.get('broll') or []) if item['at'] + item['duration'] <= total]
    overlays = []
    for overlay in new.get('overlays') or []:
        if overlay['kind'] == 'hook':
            start, end = convert(overlay['start']), min(convert(overlay['end']), total)
            if end - start < 0.1:
                continue
            overlay = {**overlay, 'start': start, 'end': end}
        overlays.append(overlay)
    result['overlays'] = overlays
    return result
