import pytest

from clippie import planner


def make_source(source_id, title, offset=0.0, duration=90.0):
    beat_text = [
        "The surprising finding changed the question for everyone.",
        "For context, the background and starting point explain why it happened.",
        "The evidence shows how the process works because each step connects.",
        "Therefore the consequence is clear and the practical takeaway matters.",
        "For example, this observation provides evidence from a real case.",
    ]
    segments = []
    for i in range(80):
        start = i * 1.0
        segments.append({"id": i, "text": beat_text[i % len(beat_text)], "start": start, "end": start + 0.9})
    return {"id": source_id, "title": title, "duration": duration, "segments": segments, "words": []}


def base_timeline():
    return {
        "schema_version": 1,
        "aspect_ratio": "16:9",
        "width": 1280,
        "height": 720,
        "fps": 30,
        "clips": [
            {"id": "c1", "source_id": "a", "start": 0, "end": 5, "role": "hook", "note": "Open", "framing": {"mode": "fit"}},
            {"id": "c2", "source_id": "a", "start": 5, "end": 10, "role": "payoff", "note": "Close", "framing": {"mode": "fit"}},
        ],
    }


def test_validate_timeline_canonicalizes_program_order_and_drops_unknown_fields():
    timeline = base_timeline()
    timeline["clips"][0]["at"] = 500
    timeline["clips"][1]["at"] = -99
    timeline["untrusted_extra"] = "discard"
    sources = {"a": {"duration": 30}}

    result = planner.validate_timeline(timeline, sources)

    assert [clip["at"] for clip in result["clips"]] == [0.0, 5.0]
    assert result["duration"] == 10.0
    assert "untrusted_extra" not in result
    assert result["aspect_ratio"] == "16:9"


@pytest.mark.parametrize("mutate, message", [
    (lambda t: t["clips"][1].update(source_id="missing"), "Unknown source_id"),
    (lambda t: t["clips"][1].update(start=29, end=31), "inside source"),
    (lambda t: t["clips"][1].update(start=4, end=8), "overlaps another speech"),
    (lambda t: t["clips"][1].update(framing={"mode": "stretch"}), "framing.mode"),
    (lambda t: t.update(aspect_ratio="16:9", width=720, height=1280), "does not match"),
    (lambda t: t.update(fps=29), "fps must"),
])
def test_validate_timeline_rejects_invalid_sources_timing_framing_and_format(mutate, message):
    timeline = base_timeline()
    mutate(timeline)

    with pytest.raises(ValueError, match=message):
        planner.validate_timeline(timeline, {"a": {"duration": 30}})


def test_validate_timeline_rejects_overlapping_title_layers_and_bad_order_role():
    timeline = base_timeline()
    timeline["clips"][0]["role"] = "random"
    with pytest.raises(ValueError, match="role must"):
        planner.validate_timeline(timeline, {"a": {"duration": 30}})

    timeline = base_timeline()
    timeline["titles"] = [{"text": "First", "at": 0, "duration": 4}, {"text": "Second", "at": 3, "duration": 2}]
    with pytest.raises(ValueError, match="cannot overlap"):
        planner.validate_timeline(timeline, {"a": {"duration": 30}})
