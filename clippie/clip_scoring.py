"""Transparent clip score (docs/v3/GENERATION_V3.md).

The score is an editorial ranking aid computed locally from five model judgments (1-5, each with a
one-line evidence note) and the clip duration. It is not a virality or performance prediction.
"""
from __future__ import annotations

import math

# 2: hook judged blind from the opening seconds; preferred length 20-45 s.
SCORE_VERSION = 2
# Fixed weights of the five component judgments; they sum to 1.
WEIGHTS = {'hook': 0.30, 'standalone': 0.20, 'payoff': 0.20, 'density': 0.15, 'shareability': 0.15}
COMPONENTS = tuple(WEIGHTS)
# Share of the 0-100 score that comes from the components; the rest is the duration fit.
COMPONENT_SHARE = 0.85
LABELS = {'hook': 'Strong opening', 'standalone': 'Makes sense on its own', 'payoff': 'Clear ending',
          'density': 'Tight pacing', 'shareability': 'Easy to share'}
WEAK = {'hook': 'Opening is soft', 'standalone': 'Needs context to land', 'payoff': 'Ending is weak',
        'density': 'Some slack or rambling', 'shareability': 'Less likely to be passed on'}
# Shorts hold best when the payoff lands fast; longer clips are allowed but rank lower.
DEFAULT_RANGE = (20.0, 45.0)


def preferred_range(max_duration=60.0, preferred_duration=None):
    """The duration band that scores full marks, from project constraints."""
    maximum = float(max_duration)
    if preferred_duration:
        centre = float(preferred_duration)
        return (round(min(centre * 0.75, maximum), 1), round(min(centre * 1.25, maximum), 1))
    return (min(DEFAULT_RANGE[0], maximum * 0.5), min(DEFAULT_RANGE[1], maximum))


def components_valid(components):
    """True when all five components carry an integer judgment 1-5."""
    return (isinstance(components, dict) and set(components) >= set(COMPONENTS)
            and all(isinstance(components[name], dict) and type(components[name].get('score')) is int
                    and 1 <= components[name]['score'] <= 5 for name in COMPONENTS))


def duration_fit(duration, rng):
    """1.0 inside the preferred range, falling linearly to 0 at half the lower bound or twice the upper."""
    low, high = rng
    if not math.isfinite(duration) or duration <= 0:
        return 0.0
    if duration < low:
        return max(0.0, 1.0 - (low - duration) / (low * 0.5)) if low > 0 else 1.0
    if duration > high:
        return max(0.0, 1.0 - (duration - high) / high) if high > 0 else 1.0
    return 1.0


def _reasons(components, fit, duration, rng):
    ranked = sorted(COMPONENTS, key=lambda n: (-components[n]['score'], -WEIGHTS[n], COMPONENTS.index(n)))
    strong = [n for n in ranked if components[n]['score'] >= 4][:3]
    if len(strong) < 2:
        strong = [n for n in ranked if components[n]['score'] >= 3][:2]
    reasons = [_line(LABELS[n], components[n]) for n in strong]
    weakest = min(COMPONENTS, key=lambda n: (components[n]['score'], -WEIGHTS[n]))
    if components[weakest]['score'] <= 3 and weakest not in strong:
        reasons.append(_line(WEAK[weakest], components[weakest]))
    elif fit < 0.8:
        side = 'short' if duration < rng[0] else 'long'
        reasons.append(f'Runs {side} for the preferred {rng[0]:g}-{rng[1]:g} s range ({duration:.0f} s)')
    return reasons


def _line(label, component):
    note = str(component.get('evidence') or '').strip().rstrip('.')
    return f'{label}: {note[:90]}' if note else label


def score(components, duration, preferred=DEFAULT_RANGE):
    """Return `{score, reasons, components, score_version, duration_fit}`; deterministic for equal input.

    score = 100 * (0.85 * sum(weight * (judgment - 1) / 4) + 0.15 * duration_fit), rounded to 0.1.
    `reasons` are the two or three strongest positives plus the main weakness, in plain language.
    """
    if not components_valid(components):
        raise ValueError('Every component needs an integer judgment from 1 to 5.')
    rng = (float(preferred[0]), float(preferred[1]))
    if not 0 < rng[0] <= rng[1]:
        raise ValueError('Preferred duration range must satisfy 0 < low <= high.')
    base = sum(WEIGHTS[n] * (components[n]['score'] - 1) / 4 for n in COMPONENTS)
    fit = duration_fit(float(duration), rng)
    total = round(100 * (COMPONENT_SHARE * base + (1 - COMPONENT_SHARE) * fit), 1)
    kept = {n: {'score': components[n]['score'], 'evidence': str(components[n].get('evidence') or '')[:200]}
            for n in COMPONENTS}
    return {'score': total, 'reasons': _reasons(components, fit, float(duration), rng), 'components': kept,
            'score_version': SCORE_VERSION, 'duration_fit': round(fit, 3)}
