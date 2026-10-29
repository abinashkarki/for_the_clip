"""Local rules for the on-screen hook line (docs/v3/GENERATION_V3.md, "Hooks").

A hook's job is to open a question or tension that the clip answers, not to summarise the speech. So it is not
required to reuse the speech's words. It must still be grounded: every number it uses must be spoken in the clip
and every name in the source (a car review may name the car even where the clip only says "it"), and it may not
simply repeat the opening words that viewers already hear and read in the captions.
Meaning-level faithfulness (no invented claims, reactions or outcomes) is checked by the review model.
"""
from __future__ import annotations

import re

HOOK_MAX_WORDS = 9
# Share of a hook's content words found in the opening speech at which it counts as a repeat.
ECHO_SHARE = 0.75
ECHO_OPENING_WORDS = 20  # about the words heard while the hook is on screen
STOP = frozenset('a an and are as at be been but by can could did do does for from had has have he her his how i if '
                 'in into is it its just me my of on or our she so that the their them then there they this to was '
                 'we were what when where which who why will with would you your not no'.split())


def _tokens(text):
    return re.findall(r"[a-z0-9]+", str(text).casefold().replace("'", '').replace('’', ''))


def _stem(token):
    for suffix in ('ing', 'ed', 'es', 's'):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[:-len(suffix)]
    return token


def check_hook(hook):
    """Format rules; returns the stripped hook or raises ValueError."""
    if not isinstance(hook, str) or not hook.strip() or len(hook) > 120:
        raise ValueError('Invalid hook line.')
    if len(hook.split()) > HOOK_MAX_WORDS:
        raise ValueError(f'Hook line must be at most {HOOK_MAX_WORDS} words.')
    return hook.strip()


def _numbers(text):
    # "$70 ,000" and "2 .4" are how ASR often spaces numbers; join digit groups before comparing.
    joined = re.sub(r'(?<=\d)\s*[,.]\s*(?=\d)', '', str(text))
    return re.findall(r'\d+', joined)


def _names(hook):
    """Capitalised words after the first, unless the hook is written in Title Case (then capitals mean nothing)."""
    words = re.findall(r"[A-Za-z][\w'’-]*", hook)
    if len(words) < 2:
        return []
    capitals = [w for w in words[1:] if w[0].isupper()]
    if len(capitals) >= 0.6 * (len(words) - 1):
        return [w for w in words if w.isupper() and len(w) > 1]  # acronyms still count in Title Case
    return [w for w in capitals if w.casefold() not in STOP]


def ungrounded(text, speech, source=''):
    """Numbers the clip's `speech` never says and names neither it nor the wider `source` transcript says."""
    spoken_numbers = set(_numbers(speech))
    spoken = {_stem(t) for t in _tokens(speech + ' ' + str(source or ''))}
    missing = [n for n in _numbers(text) if n not in spoken_numbers]
    for name in _names(str(text)):
        parts = [_stem(t) for t in _tokens(name)]
        if parts and not all(p in spoken for p in parts):
            missing.append(name)
    return list(dict.fromkeys(missing))


def echoes_opening(hook, opening):
    """True when the hook mostly repeats the first spoken words, adding nothing the viewer is not already given."""
    content = [_stem(t) for t in _tokens(hook) if t not in STOP]
    if len(content) < 2:
        return False
    heard = {_stem(t) for t in _tokens(' '.join(str(opening).split()[:ECHO_OPENING_WORDS]))}
    return sum(t in heard for t in content) / len(content) >= ECHO_SHARE


def problems(hook, speech, opening, source=''):
    """Every local hook problem, in plain language. `source` is the whole source transcript, for names."""
    found = []
    try:
        check_hook(hook)
    except ValueError as error:
        return [str(error)]
    missing = ungrounded(hook, speech, source)
    if missing:
        found.append('Hook uses numbers or names the source never says: ' + ', '.join(missing[:4]) + '.')
    if echoes_opening(hook, opening):
        found.append('Hook repeats the opening words viewers already hear; it should open a question instead.')
    return found
