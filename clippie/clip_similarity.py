"""Local near-duplicate detection between candidate clips. No model calls.

Two clips are the same idea when any of these hold:
- they share most of their speech (unit overlap),
- their title, angle and hook use mostly the same content words,
- their speech is about the same specific things (TF-IDF cosine over the selected speech, so rare
  shared terms such as "barium" or "planarian" count far more than common words), backed by at least
  some overlap in how the clip is described.

Thresholds are tuned to keep distinct points from one long talk apart while catching the same anecdote
told twice or picked twice by different discovery windows.
"""
from __future__ import annotations

from collections import Counter
import math
import re

UNIT_OVERLAP = 0.5        # share of the shorter clip's speech units
LABEL_JACCARD = 0.5       # title + angle + hook content words
SPEECH_COSINE = 0.55      # speech alone is enough when very similar
SPEECH_WITH_LABEL = (0.35, 0.2)   # moderately similar speech plus some shared description
MIN_SPEECH_TERMS = 20     # speech comparison is meaningless for a few words

STOP = frozenset("""
a an and are as at be been being but by can could did do does doing for from had has have having he her his how
i if in into is it its just like more most much my no not of on or our out over so some than that the their them
then there these they this those through to too up us very was we were what when where which while who why will
with would you your about after again also any because before both each few further here itself let me might
must new now off once only other own same should since still such themselves until upon whom yet one two get
gets got really actually thing things way ways make makes made going go goes know think say says said see
""".split())
WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _stem(word):
    for suffix in ('ations', 'ation', 'ments', 'ment', 'ings', 'ing', 'ies', 'ied', 'ers', 'er', 'ed', 'es', 's'):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[:-len(suffix)] + ('y' if suffix in ('ies', 'ied') else '')
    return word


def terms(text):
    return [_stem(w) for w in WORD.findall(str(text).casefold()) if w not in STOP and len(w) > 2]


def label(candidate):
    return set(terms(' '.join(str(candidate.get(k, '')) for k in ('title', 'angle', 'hook_line'))))


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


class Corpus:
    """IDF weights over the speech and descriptions of every clip being compared, so shared rare terms dominate.

    A word every clip in the slate uses ("AI" across an AI interview) carries no weight in the description
    check, so it can never make two clips look alike on its own.
    """

    def __init__(self, speeches, labels=()):
        self.docs = [Counter(terms(text)) for text in speeches]
        frequency = Counter(term for doc in self.docs for term in doc)
        count = max(1, len(self.docs))
        self.idf = {term: math.log((1 + count) / (1 + n)) + 1 for term, n in frequency.items()}
        labels = list(labels)
        label_df = Counter(term for words in labels for term in words)
        self.label_count = len(labels)
        self.label_idf = {term: math.log((1 + len(labels)) / (1 + n)) for term, n in label_df.items()}

    def label_similarity(self, a, b):
        if not self.label_count or not a or not b:
            return jaccard(a, b)
        weight = lambda term: self.label_idf.get(term, math.log(1 + self.label_count))
        union = sum(weight(t) for t in a | b)
        return sum(weight(t) for t in a & b) / union if union else 0.0

    def vector(self, text):
        doc = Counter(terms(text))
        return {t: (1 + math.log(c)) * self.idf.get(t, math.log(1 + len(self.docs)) + 1) for t, c in doc.items()}


def cosine(a, b):
    dot = sum(v * b.get(t, 0.0) for t, v in a.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def speech_of(candidate, units):
    return ' '.join(units[i]['text'] for i in candidate.get('unit_ids', []) if i in units)


def same_idea(a, b, units, corpus=None, vectors=None):
    """Return a short reason string when a and b are near-duplicates, else None."""
    ua, ub = set(a.get('unit_ids') or []), set(b.get('unit_ids') or [])
    shared = len(ua & ub)
    if ua and ub and ((shared >= 2 and shared / min(len(ua), len(ub)) >= UNIT_OVERLAP)
                      or shared / max(len(ua), len(ub)) > UNIT_OVERLAP):
        return 'shares most of its speech'
    labels = corpus.label_similarity(label(a), label(b)) if corpus is not None else jaccard(label(a), label(b))
    if labels >= LABEL_JACCARD:
        return 'describes the same point'
    if units is None:
        return None
    if corpus is None:
        corpus = Corpus([speech_of(a, units), speech_of(b, units)])
    va = (vectors or {}).get(a.get('id')) or corpus.vector(speech_of(a, units))
    vb = (vectors or {}).get(b.get('id')) or corpus.vector(speech_of(b, units))
    if min(len(va), len(vb)) < MIN_SPEECH_TERMS:
        return None
    speech = cosine(va, vb)
    if speech >= SPEECH_COSINE or (speech >= SPEECH_WITH_LABEL[0] and labels >= SPEECH_WITH_LABEL[1]):
        return 'covers the same material'
    return None


def prepare(candidates, units):
    """Corpus and per-candidate vectors for repeated comparisons within one slate."""
    corpus = Corpus([speech_of(c, units) for c in candidates], [label(c) for c in candidates])
    return corpus, {c.get('id'): corpus.vector(speech_of(c, units)) for c in candidates}
