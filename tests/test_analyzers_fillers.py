import pytest

from clippie.analyzers import fillers


def words(spec, gap=0.05, dur=0.3, start=1.0):
    """spec: text or (text, gap_before). Returns word dicts with ids 0..n-1."""
    out, t = [], start
    for i, item in enumerate(spec.split() if isinstance(spec, str) else spec):
        text, before = (item, gap) if isinstance(item, str) else item
        t += before
        out.append({"id": i, "text": text, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
    return out


def cut_ids(result, reason=None):
    return [c["word_ids"] for c in result if reason is None or c["reason"] == reason]


def assert_valid(result):
    last = 0.0
    for c in result:
        assert c["reason"] in {"filler", "silence", "repeat"}
        assert c["end"] - c["start"] >= 0.04 - 1e-9
        assert c["start"] >= last - 1e-9
        last = c["end"]


@pytest.mark.parametrize("filler", ["um", "uh", "erm", "ah", "hmm", "mm", "umm", "uhh", "Um,", "UH.", "hmmm", "mmm", "uhm"])
def test_standalone_fillers_are_cut(filler):
    ws = words(f"so I think {filler} we should go")
    result = fillers.detect(ws)
    assert cut_ids(result, "filler") == [[3]]
    assert_valid(result)


def test_filler_cut_stays_inside_the_filler_word():
    ws = words("it works um fine")
    (cut,) = fillers.detect(ws)
    um = ws[2]
    assert um["start"] <= cut["start"] < cut["end"] <= um["end"]


def test_filler_cut_is_clamped_to_overlapping_neighbours():
    ws = [{"id": 0, "text": "so", "start": 1.0, "end": 1.35},
          {"id": 1, "text": "um", "start": 1.3, "end": 1.7},
          {"id": 2, "text": "yes", "start": 1.65, "end": 2.0}]
    (cut,) = fillers.detect(ws)
    assert cut["start"] >= 1.35 and cut["end"] <= 1.65


@pytest.mark.parametrize("text", ["like", "you know", "basically", "so", "well", "I mean", "right"])
def test_discourse_words_are_not_fillers_by_default(text):
    assert fillers.detect(words(f"it was {text} fine")) == []


@pytest.mark.parametrize("text", ["number", "umbrella", "album", "human", "ahead", "hummus", "uhura", "mama", "hum"])
def test_words_containing_filler_letters_are_untouched(text):
    assert fillers.detect(words(f"the {text} is here")) == []


@pytest.mark.parametrize("text", ["I don't know um", "um I do not agree", "um never mind", "I can't um say no", "not um bad"])
def test_negations_survive_filler_removal(text):
    ws = words(text)
    result = fillers.detect(ws)
    cut_words = {i for ids in cut_ids(result) for i in ids}
    for w in ws:
        if w["text"].lower() in {"don't", "not", "never", "can't", "no", "do"}:
            assert w["id"] not in cut_words
    # and no cut overlaps a negation's time span
    for w in ws:
        if w["text"].lower() in {"don't", "not", "never", "can't", "no"}:
            assert all(c["end"] <= w["start"] + 1e-9 or c["start"] >= w["end"] - 1e-9 for c in result)


def test_negation_repeats_are_never_cut():
    for text in ["no no no", "not not", "never never", "I don't don't"]:
        assert fillers.detect(words(text)) == []


@pytest.mark.parametrize("text,ids", [("I I think so", [[0]]), ("the the cat sat", [[0]]), ("we we we agree", [[0], [1]]),
                                       ("and and then", [[0]]), ("I I I I", [[0], [1], [2]])])
def test_stutter_repeats(text, ids):
    result = fillers.detect(words(text))
    assert cut_ids(result, "repeat") == ids
    assert_valid(result)


@pytest.mark.parametrize("text", ["very very good", "that that is odd", "had had enough", "bye bye now", "so so", "really really",
                                  "big big house", "go go go", "yes yes"])
def test_legitimate_repeats_and_content_words_are_kept(text):
    assert fillers.detect(words(text)) == []


def test_repeat_across_a_long_pause_is_not_a_stutter():
    ws = words(["I", ("I", 0.7), "think"], dur=0.2)
    assert cut_ids(fillers.detect(ws, silence_min=2.0), "repeat") == []


def test_word_fragment_stutter():
    ws = words("w- we should go")
    assert cut_ids(fillers.detect(ws), "repeat") == [[0]]
    assert fillers.detect(words("well- known fact")) == []  # fragment of a different word


def test_silence_cut_keeps_padding_each_side():
    ws = words(["hello", ("world", 1.0)])
    (cut,) = fillers.detect(ws, silence_min=0.6, keep_padding=0.08)
    assert cut["reason"] == "silence" and cut["word_ids"] == []
    assert cut["start"] == pytest.approx(ws[0]["end"] + 0.08, abs=1e-3)
    assert cut["end"] == pytest.approx(ws[1]["start"] - 0.08, abs=1e-3)


def test_gap_below_silence_min_is_kept():
    assert fillers.detect(words(["hello", ("world", 0.59)]), silence_min=0.6) == []


def test_silence_too_short_after_padding_is_dropped():
    assert fillers.detect(words(["a", ("b", 0.2)]), silence_min=0.2, keep_padding=0.1) == []


def test_mixed_transcript_is_sorted_non_overlapping():
    ws = words(["So", "um", ("I", 0.9), "I", "think", "uh", ("not", 0.7), "that", "um"])
    result = fillers.detect(ws)
    assert {c["reason"] for c in result} == {"filler", "silence", "repeat"}
    assert [c["start"] for c in result] == sorted(c["start"] for c in result)
    assert_valid(result)


@pytest.mark.parametrize("text", ["uh huh", "uh oh", "mm hmm", "ah ha"])
def test_backchannels_are_not_fillers(text):
    assert fillers.detect(words(text)) == []


def test_empty_and_malformed_input():
    assert fillers.detect([]) == []
    assert fillers.detect(None) == []
    bad = [{"text": "um"}, {"text": "uh", "start": 2, "end": 1}, {"text": "x", "start": "a", "end": 3},
           {"text": "", "start": 1, "end": 2}]
    assert fillers.detect(bad) == []


def test_unsorted_input_and_word_id_key():
    ws = [{"word_id": 7, "text": "ok", "start": 2.5, "end": 2.7}, {"word_id": 5, "text": "um", "start": 2.0, "end": 2.3}]
    (cut,) = fillers.detect(ws)
    assert cut["word_ids"] == [5]


def test_other_language_fillers_and_unknown_language():
    assert cut_ids(fillers.detect(words("das ist äh gut"), language="de"), "filler") == [[2]]
    assert cut_ids(fillers.detect(words("c'est euh bon"), language="fr-FR"), "filler") == [[1]]
    assert fillers.detect(words("um okay"), language="ja") == []  # no filler list: nothing guessed


def test_cuts_for_segment_clips_strictly_inside():
    cuts = [{"start": 4.0, "end": 5.0, "reason": "filler", "word_ids": [1]},
            {"start": 9.99, "end": 10.5, "reason": "silence", "word_ids": []},
            {"start": 12.0, "end": 12.3, "reason": "repeat", "word_ids": [3]}]
    out = fillers.cuts_for_segment(cuts, 4.0, 10.0)
    assert out == [{"start": 4.001, "end": 5.0, "reason": "filler"}]
    assert all(set(c) == {"start", "end", "reason"} for c in out)
