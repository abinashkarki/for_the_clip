"""Transcript-only filler, stutter and silence detection. Pure Python, no vision dependency.

Output ranges are in source seconds and are valid timeline v3 cuts once clipped to a
segment (see `cuts_for_segment`): sorted, non-overlapping, each at least MIN_CUT long.
Only filler tokens, immediate stutter repeats of function words and long gaps are
ever cut; every other word, negations included, is left untouched.
"""
from __future__ import annotations

import re

MIN_CUT = 0.04
REPEAT_GAP_MAX = 0.6

_FILLERS = {
    "en": r"u+m+|u+h+m*|e+r+m+|a+h+|h+m+|m{2,}|uhm+",
    "de": r"[aä]+h+m*|[oö]+h+m*|ehm+|hm+",
    "fr": r"e+u+h+|h+e+u+|hm+|mm+",
    "es": r"e+h+|e+m+|mm+|hm+",
}
# Words that may be doubled by a stutter. Content words, negations, intensifiers
# ("very very", "no no") and legitimate doubles ("that that", "had had") are excluded.
_REPEATABLE = {
    "en": {"i", "a", "an", "the", "and", "but", "or", "if", "in", "on", "at", "of", "to", "for", "with",
           "we", "you", "he", "she", "it", "they", "my", "your", "our", "is", "are", "was", "be", "this",
           "as", "by", "from", "i'm", "i'll", "i've", "it's", "that's"},
}
_NEGATIONS = {"no", "not", "never", "none", "nor", "nobody", "nothing", "neither", "nowhere", "without",
              "don't", "doesn't", "didn't", "can't", "cannot", "won't", "wouldn't", "shouldn't",
              "couldn't", "isn't", "aren't", "wasn't", "weren't", "haven't", "hasn't", "hadn't"}
# Backchannels that start with a filler-looking token: "uh huh", "uh oh", "mm hmm", "ah ha".
_BACKCHANNEL_NEXT = {"huh", "oh", "hmm", "mm", "ha", "mhm", "hm"}


def _token(text) -> str:
    text = str(text or "").lower().replace("’", "'")
    return re.sub(r"^[^\w']+|[^\w']+$", "", text, flags=re.UNICODE)


def _clean_words(words) -> list[dict]:
    rows = []
    for index, word in enumerate(words or []):
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (start >= 0 and end > start):
            continue
        word_id = word.get("id", word.get("word_id", index))
        rows.append({"id": word_id, "text": word.get("text", word.get("word", "")),
                     "token": _token(word.get("text", word.get("word", ""))), "start": start, "end": end})
    rows.sort(key=lambda r: (r["start"], r["end"]))
    return rows


def _is_filler(rows, index, pattern) -> bool:
    token = rows[index]["token"]
    if not token or not pattern.fullmatch(token):
        return False
    nxt = rows[index + 1]["token"] if index + 1 < len(rows) else ""
    return nxt not in _BACKCHANNEL_NEXT


def _filler_cuts(rows, language) -> list[dict]:
    pattern = re.compile(_FILLERS.get(language, ""), re.IGNORECASE) if language in _FILLERS else None
    if pattern is None:
        return []
    cuts = []
    for i, row in enumerate(rows):
        if not _is_filler(rows, i, pattern):
            continue
        # A word that precedes a backchannel partner ("mm hmm") was skipped above;
        # the partner itself must not be cut either.
        if i > 0 and rows[i - 1]["token"] and pattern.fullmatch(rows[i - 1]["token"]) and row["token"] in _BACKCHANNEL_NEXT:
            continue
        lo = rows[i - 1]["end"] if i > 0 else 0.0
        hi = rows[i + 1]["start"] if i + 1 < len(rows) else row["end"]
        cuts.append({"start": max(row["start"], lo), "end": min(row["end"], max(hi, row["start"])),
                     "reason": "filler", "word_ids": [row["id"]]})
    return cuts


def _is_fragment(rows, i) -> bool:
    """A short word fragment ending in a hyphen that the next word completes: "w- we"."""
    text = str(rows[i]["text"]).strip().lower()
    if i + 1 >= len(rows) or not re.search(r"\w{1,3}[-–—]$", text):
        return False
    stem = re.sub(r"[-–—]+$", "", text)
    return bool(stem) and rows[i + 1]["token"].startswith(stem)


def _repeat_cuts(rows, language) -> list[dict]:
    repeatable = _REPEATABLE.get(language, set())
    cuts = []
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        if b["start"] - a["end"] > REPEAT_GAP_MAX or a["token"] in _NEGATIONS:
            continue
        same = a["token"] and a["token"] == b["token"] and a["token"] in repeatable
        if not (same or _is_fragment(rows, i)):
            continue
        lo = rows[i - 1]["end"] if i > 0 else 0.0
        cuts.append({"start": max(a["start"], lo), "end": min(a["end"], b["start"]) if b["start"] > a["start"] else a["end"],
                     "reason": "repeat", "word_ids": [a["id"]]})
    return cuts


def _silence_cuts(rows, silence_min, keep_padding) -> list[dict]:
    cuts = []
    for a, b in zip(rows, rows[1:]):
        if b["start"] - a["end"] >= silence_min:
            cuts.append({"start": a["end"] + keep_padding, "end": b["start"] - keep_padding,
                         "reason": "silence", "word_ids": []})
    return cuts


def detect(words, *, language="en", silence_min=0.6, keep_padding=0.08) -> list[dict]:
    """Cut ranges for fillers, stutter repeats and silences, sorted and non-overlapping."""
    lang = str(language or "en").lower().split("-")[0].split("_")[0]
    rows = _clean_words(words)
    cuts = _filler_cuts(rows, lang) + _repeat_cuts(rows, lang) + _silence_cuts(rows, silence_min, keep_padding)
    cuts.sort(key=lambda c: (c["start"], c["end"]))
    out = []
    for cut in cuts:
        start, end = round(cut["start"], 3), round(cut["end"], 3)
        if out and start < out[-1]["end"]:
            start = out[-1]["end"]  # defensive: ranges never overlap
        if end - start < MIN_CUT - 1e-9:
            continue
        out.append({"start": start, "end": end, "reason": cut["reason"], "word_ids": list(cut["word_ids"])})
    return out


def cuts_for_segment(cuts, seg_start: float, seg_end: float) -> list[dict]:
    """Clip detected ranges to a segment as timeline cuts, strictly inside [seg_start, seg_end]."""
    eps = 0.001
    out = []
    for cut in cuts:
        start, end = max(cut["start"], seg_start + eps), min(cut["end"], seg_end - eps)
        if end - start >= MIN_CUT:
            out.append({"start": round(start, 3), "end": round(end, 3), "reason": cut["reason"]})
    return out
