import pytest

from clippie import selection


def words_for(texts, step=1.0, duration=0.8):
    return [
        {"id": i, "text": text, "start": i * step, "end": i * step + duration}
        for i, text in enumerate(texts)
    ]


def test_build_segments_uses_sentence_and_pause_boundaries():
    words = [
        {"id": 10, "text": "Hello", "start": 0.0, "end": 0.3},
        {"id": 11, "text": "there.", "start": 0.31, "end": 0.6},
        {"id": 12, "text": "Next", "start": 0.7, "end": 0.9},
        {"id": 13, "text": "thought", "start": 2.0, "end": 2.4},
    ]

    segments = selection.build_segments(words)

    assert [s["text"] for s in segments] == ["Hello there.", "Next", "thought"]
    assert [(s["start_word_id"], s["end_word_id"]) for s in segments] == [(10, 11), (12, 12), (13, 13)]


def test_heuristic_is_labeled_and_includes_transcript_tail():
    words = words_for(["solar" if i > 29 else "ordinary" for i in range(90)])

    result = selection.select_clips(words, "solar", min_duration=25, max_duration=35, max_clips=5)

    assert result["metrics"]["provider"] == "heuristic baseline"
    assert "not virality" in result["metrics"]["score_label"]
    assert all("Heuristic baseline" in clip["reason"] for clip in result["clips"])
    assert any(clip["end_word_id"] == 89 for clip in result["clips"])
    assert result["raw"] == []


def test_deepseek_maps_segment_ids_and_deduplicates_overlapping_windows(monkeypatch):
    words = words_for([f"word{i}." for i in range(4200)], step=0.5, duration=0.4)
    calls = []

    def fake_post(window, brief, api_key, model, min_duration, max_duration, max_clips, correction=None):
        calls.append([s["id"] for s in window])
        if len(calls) == 1:
            a, b = 1480, 1539
        elif len(calls) == 2:
            a, b = 1480, 1539  # duplicate suggestion in the overlap
        else:
            a, b = 4000, 4059  # proves the final context window was processed
        return ([{
            "start_segment_id": a,
            "end_segment_id": b,
            "title": "A grounded excerpt",
            "reason": "The transcript states this clearly.",
            "score": 0.72,
        }], {"choices": [{"message": {"content": "raw"}}]})

    monkeypatch.setattr(selection, "_post_window", fake_post)
    result = selection.select_clips(words, "brief", provider="deepseek", min_duration=25, max_duration=35, api_key="secret")

    assert len(calls) == 3
    assert calls[0][-1] >= 1480 and calls[1][0] <= 1480
    segments = selection.build_segments(words)
    assert segments[calls[0][-1]]["end"] - segments[calls[1][0]]["start"] >= 60.0
    assert calls[-1][-1] == 4199  # the tail was in a provider context window
    assert len(result["raw"]) == 3
    assert len(result["clips"]) == 2  # duplicate range was globally collapsed
    assert [(c["start_word_id"], c["end_word_id"]) for c in result["clips"]] == [(1480, 1539), (4000, 4059)]
    assert result["clips"][0]["start"] == words[1480]["start"]
    assert result["clips"][0]["end"] == words[1539]["end"]
    assert result["clips"][0]["end"] - result["clips"][0]["start"] == pytest.approx(29.9)


def test_deepseek_rejects_invalid_response_and_keeps_raw_without_fallback(monkeypatch):
    raw = {"choices": [{"message": {"content": "not json"}}]}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return raw

    monkeypatch.setattr(selection.httpx, "post", lambda *args, **kwargs: FakeResponse())
    with pytest.raises(RuntimeError, match="invalid response"):
        selection.select_clips(words_for(["hello"] * 100), "brief", provider="deepseek", min_duration=20, api_key="secret")


def test_deepseek_request_includes_duration_metadata_rubric_and_bounded_effort(monkeypatch):
    words = words_for([f"word{i}." for i in range(40)], step=1.0, duration=0.8)
    request = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"clips": []}'}}], "usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}}

    def fake_post(url, **kwargs):
        request.update(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr(selection.httpx, "post", fake_post)
    result = selection.select_clips(words, "brief", provider="deepseek", min_duration=20, max_duration=40, max_clips=3, api_key="key")

    prompt = request["messages"][1]["content"]
    assert '"start": 0.0' in prompt and '"duration":' in prompt
    assert "20 to 40 seconds" in prompt and "hook" in prompt and "payoff" in prompt
    assert request["thinking"] == {"type": "disabled"}
    assert request["reasoning_effort"] == "none"
    assert request["max_tokens"] == 1200
    assert result["metrics"]["total_tokens"] == 100


def test_provider_model_timestamps_are_ignored_and_local_word_times_are_used(monkeypatch):
    words = words_for([f"word{i}." for i in range(60)], step=0.5, duration=0.4)
    monkeypatch.setattr(selection, "_post_window", lambda *args: ([{
        "start_segment_id": 0,
        "end_segment_id": 59,
        "start": 0.0,
        "end": 30.0,
        "title": "Invalid range",
        "reason": "Should be rejected",
        "score": 0.9,
    }], {"raw": True}))

    result = selection.select_clips(words, "brief", provider="deepseek", min_duration=20, max_duration=40, api_key="secret")

    assert len(result["clips"]) == 1
    assert result["clips"][0]["start"] == 0.0
    assert result["clips"][0]["end"] == pytest.approx(29.9)
    assert len(result["raw"]) == 1
    assert result["metrics"]["provider_requests"] == 1


def test_provider_rejects_segment_ids_outside_context_window(monkeypatch):
    words = words_for([f"word{i}." for i in range(60)], step=0.5, duration=0.4)
    monkeypatch.setattr(selection, "_post_window", lambda *args: ([{
        "start_segment_id": 0,
        "end_segment_id": 99,
        "title": "Invalid range",
        "reason": "Should be rejected",
        "score": 0.9,
    }], {"raw": True}))

    result = selection.select_clips(words, "brief", provider="deepseek", min_duration=20, max_duration=40, api_key="secret")

    assert result["clips"] == []
    assert any("belong to the supplied context window" in warning for warning in result["warnings"])
    assert len(result["raw"]) == 2  # one bounded correction retry
    assert result["metrics"]["provider_requests"] == 2


def test_deepseek_boundary_durations_are_checked_from_local_words(monkeypatch):
    words = words_for([f"word{i}." for i in range(40)], step=1.0, duration=0.8)
    monkeypatch.setattr(selection, "_post_window", lambda *args: ([{
        "start_segment_id": 0,
        "end_segment_id": 29,
        "title": "Boundary",
        "reason": "Boundary example",
        "score": 0.5,
    }], {"raw": "saved"}))

    result = selection.select_clips(words, "brief", provider="deepseek", min_duration=29.8, max_duration=29.8, api_key="key")

    assert len(result["clips"]) == 1
    assert result["clips"][0]["start"] == 0.0
    assert result["clips"][0]["end"] == pytest.approx(29.8)

