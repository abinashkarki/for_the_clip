"""Local word-edge proofs. Models supply offsets; source words alone own text and time.

Two bounded span kinds extend the default three-word edge trim:
- `lead`: drops a longer spoken lead-in at the head of a clip's first unit ("So, one thing I'd say is that ...").
  The dropped words (at most fourteen) may not hold a negation or qualification, and at least four words must remain.
- `teaser`: a short verbatim phrase from a later unit, played first as a cold open (at most TEASER_MAX_SECONDS).
"""
import copy
import json
import re

TEASER_MAX_SECONDS = 4.5
TEASER_MIN_WORDS = 3
LEAD_MIN_KEPT = 4
# A lead-in is bounded by length, not share: ASR sentences are often a long lead-in plus a short claim.
LEAD_MAX_DROPPED = 14
# Dropping any of these could change what the kept words mean, so a lead trim may never remove them.
LEAD_GUARD = frozenset("not no never nor none nothing cannot without unless except if maybe might perhaps probably possibly "
                       "doubt hardly barely only almost unlikely".split())


def artifacts(evidences):
    if isinstance(evidences, dict):
        evidences = [evidences] if 'artifact' in evidences or 'manifest' in evidences else list(evidences.values())
    return {a['manifest']['source_id']: a for e in evidences for a in [e.get('artifact', e)]}


def resolve_selection(unit, artifact, row):
    """Return a resolved unit and canonical proof, or no proof for a whole unit."""
    ids = unit.get('word_ids', [])
    source_words = artifact['transcript']['words']
    word_ids = [w['id'] for w in source_words]
    if len(set(word_ids)) != len(word_ids) or len(set(ids)) != len(ids) or not set(ids) <= set(word_ids):
        raise ValueError('Saved word identities are missing or duplicated.')
    has_start, has_end = 'start_word' in row, 'end_word' in row
    if has_start != has_end:
        raise ValueError('Both word-edge offsets are required.')
    kind = row.get('kind')
    if kind not in (None, 'lead', 'teaser'):
        raise ValueError('Unknown word-span kind.')
    if not has_start:
        if kind:
            raise ValueError('A lead or teaser span needs word offsets.')
        return copy.deepcopy(unit), None
    start, end = row['start_word'], row['end_word']
    if type(start) is not int or type(end) is not int or not ids:
        raise ValueError('Word-edge offsets require integer offsets and saved source words.')
    words = {w['id']: w for w in artifact['transcript']['words']}
    if kind == 'lead':
        _check_lead(ids, words, start, end)
    elif kind == 'teaser':
        if not 0 <= start <= end < len(ids) or end - start + 1 < TEASER_MIN_WORDS:
            raise ValueError(f'A teaser must be at least {TEASER_MIN_WORDS} consecutive words of one unit.')
        if words[ids[end]]['end'] - words[ids[start]]['start'] > TEASER_MAX_SECONDS + 1e-6:
            raise ValueError(f'A teaser must last at most {TEASER_MAX_SECONDS:g} seconds.')
    elif not 0 <= start <= end < len(ids) or start > 3 or len(ids) - end - 1 > 3:
        raise ValueError('Word-edge range must stay in one unit and omit at most three words per edge.')
    if start == 0 and end == len(ids) - 1 and kind != 'teaser':
        return copy.deepcopy(unit), None
    selected = [words[i] for i in ids[start:end + 1]]
    left, right = selected[0]['start'], selected[-1]['end']
    if not unit['start'] <= left < right <= unit['end']:
        raise ValueError('Word-edge times must remain inside their parent unit.')
    if (any(words[i]['end'] > left for i in ids[:start])
            or any(words[i]['start'] < right for i in ids[end + 1:])
            or any(w['end'] > right for w in selected)):
        raise ValueError('Overlapping source word times make this cut boundary unsafe.')
    text = ' '.join(w['text'] for w in selected)
    proof = {'start_word': start, 'end_word': end, 'source_fingerprint': artifact['manifest']['fingerprint'],
             'word_ids': ids[start:end + 1], 'start': left, 'end': right, 'text': text,
             **({'kind': kind} if kind else {})}
    resolved = copy.deepcopy(unit)
    resolved.update(start=left, end=right, text=text, word_ids=ids[start:end + 1])
    resolved['warnings'] = list(unit.get('warnings', [])) + [
        'Word-edge trim uses derived ASR alignment; listen to verify the cut and preserved meaning.']
    return resolved, proof


def resolve_plan(plan, evidences):
    """Verify saved proof and exact timeline, preserving legacy whole-unit plans."""
    source_artifacts = artifacts(evidences)
    units = {u['id']: u for a in source_artifacts.values() for u in a['units']}
    ids = plan.get('unit_ids')
    spans = plan.get('word_spans', {})
    if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
            or not isinstance(spans, dict) or set(spans) - set(ids)):
        raise ValueError('Invalid saved speech unit/span references.')
    provenance = plan.get('evidence_provenance', {}).get('sources', {})
    if provenance != {sid: a['manifest']['fingerprint'] for sid, a in source_artifacts.items()}:
        raise ValueError('Saved speech source fingerprint changed.')
    resolved = []
    for identity in ids:
        if identity not in units:
            raise ValueError('Unknown saved speech unit.')
        unit = units[identity]
        span = spans.get(identity)
        if span is not None and not isinstance(span, dict):
            raise ValueError('Invalid saved word span.')
        row = {} if span is None else {k: span[k] for k in ('start_word', 'end_word', 'kind') if k in span}
        result, proof = resolve_selection(unit, source_artifacts[unit['source_id']], row)
        if span is not None and (proof is None or json.dumps(span, sort_keys=True, allow_nan=False) != json.dumps(proof, sort_keys=True, allow_nan=False)):
            raise ValueError('Saved word span differs from exact source proof.')
        resolved.append(result)
    clips = plan.get('timeline', {}).get('clips', [])
    if len(clips) != len(resolved) or any((u['source_id'], u['start'], u['end']) !=
            (c.get('source_id'), c.get('start'), c.get('end')) for u, c in zip(resolved, clips)):
        raise ValueError('Timeline no longer resolves exact evidence IDs and word spans.')
    return resolved


def projected_unit(unit, artifact):
    """Bounded word choices; opaque word identities and full word arrays stay local."""
    result = {k: copy.deepcopy(v) for k, v in unit.items() if k not in ('word_ids', 'segment_ids')}
    ids = unit.get('word_ids', [])
    words = {w['id']: w for w in artifact['transcript']['words']}
    result['word_count'] = len(ids)
    result['head_choices'] = [{'index': i, 'text': words[ids[i]]['text']} for i in range(min(4, len(ids)))]
    result['tail_choices'] = [{'index': i, 'text': words[ids[i]]['text']} for i in range(max(0, len(ids) - 4), len(ids))]
    return result


def _norm(text):
    return re.sub(r"[^\w]", '', str(text).casefold().replace('\u2019', "'").replace("'", ''))


def _check_lead(ids, words, start, end):
    if end != len(ids) - 1 or not 1 <= start or len(ids) - start < LEAD_MIN_KEPT:
        raise ValueError(f'A lead trim drops head words only and keeps at least {LEAD_MIN_KEPT} words.')
    if start > LEAD_MAX_DROPPED:
        raise ValueError(f'A lead trim may drop at most {LEAD_MAX_DROPPED} words.')
    dropped = [str(words[i]['text']) for i in ids[:start]]
    guarded = [w for w in dropped if _norm(w) in LEAD_GUARD or "n't" in w.casefold().replace('\u2019', "'")]
    if guarded:
        raise ValueError('A lead trim may not drop a negation or qualification: ' + ', '.join(guarded[:3]) + '.')


def locate_phrase(unit, artifact, phrase):
    """Word offsets (start, end) of the first verbatim occurrence of `phrase` in `unit`, else None.

    Matching ignores case and punctuation, so ASR spacing such as "2 .4 -liter" still matches "2.4-liter".
    """
    ids = unit.get('word_ids') or []
    words = {w['id']: w for w in artifact['transcript']['words']}
    tokens = [(index, _norm(words[i]['text'])) for index, i in enumerate(ids)]
    tokens = [(index, token) for index, token in tokens if token]
    wanted = [t for t in (_norm(part) for part in str(phrase).split()) if t]
    if not wanted:
        return None
    joined_wanted = ''.join(wanted)
    for first in range(len(tokens)):
        built = ''
        for last in range(first, len(tokens)):
            built += tokens[last][1]
            if built == joined_wanted:
                return tokens[first][0], tokens[last][0]
            if not joined_wanted.startswith(built):
                break
    return None


def lead_span(unit, artifact, open_from):
    """Lead-trim row for a clip that should start at `open_from` inside its first unit; None when no trim applies.

    Raises ValueError when the phrase is missing or the trim would be unsafe.
    """
    found = locate_phrase(unit, artifact, open_from)
    if found is None:
        raise ValueError('open_from is not a verbatim phrase of the first unit.')
    start = found[0]
    if start == 0:
        return None
    row = {'start_word': start, 'end_word': len(unit['word_ids']) - 1, 'kind': 'lead'}
    resolve_selection(unit, artifact, row)
    return row


def teaser_span(unit, artifact, text):
    """Teaser row for a verbatim phrase of `unit`; raises ValueError when missing or too long."""
    found = locate_phrase(unit, artifact, text)
    if found is None:
        raise ValueError('Teaser text is not a verbatim phrase of its unit.')
    row = {'start_word': found[0], 'end_word': found[1], 'kind': 'teaser'}
    resolve_selection(unit, artifact, row)
    return row
