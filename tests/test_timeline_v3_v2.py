"""from_v2: lossless conversion of every v2 field."""

from __future__ import annotations

import pytest

from clippie import planner, timeline_v3 as tl

SOURCES = {"a": {"duration": 300.0, "words": [{"id": i, "text": f"w{i}", "start": 10 + i * 0.4, "end": 10.3 + i * 0.4}
                                               for i in range(80)]},
           "b": {"duration": 60.0}}


def v2(**changes):
    base = {"schema_version": 2, "clips": [{"id": "c1", "source_id": "a", "start": 10, "end": 25},
                                           {"id": "c2", "source_id": "a", "start": 40, "end": 50}]}
    base.update(changes)
    return base


def test_clips_become_segments_and_shots_in_program_order():
    out = tl.from_v2(v2(), SOURCES)
    assert [(s["id"], s["source_id"], s["start"], s["end"], s["cuts"]) for s in out["segments"]] == [
        ("c1", "a", 10.0, 25.0, []), ("c2", "a", 40.0, 50.0, [])]
    assert [(s["start"], s["end"], s["layout"]) for s in out["shots"]] == [(0.0, 15.0, "fit"), (15.0, 25.0, "fit")]
    assert out["canvas"]["width"] == 1080 and out["canvas"]["height"] == 1920 and out["canvas"]["fps"] == 30
    assert tl.duration(out) == pytest.approx(25.0)
    assert out["audio"] == {"normalize": True} and out["captions"]["enabled"] is True


def test_fill_framing_becomes_fill_keyframe_and_fit_keeps_zoom():
    out = tl.from_v2(v2(clips=[
        {"id": "c1", "source_id": "a", "start": 0, "end": 5, "framing": {"mode": "fill", "x": 0.3, "y": 0.6, "zoom": 1.2}},
        {"id": "c2", "source_id": "a", "start": 5, "end": 10, "framing": {"mode": "fit", "zoom": 1.1}}]), SOURCES)
    fill, fit = out["shots"]
    assert fill["layout"] == "fill"
    frame = fill["regions"][0]["keyframes"][0]
    assert (frame["t"], frame["x"], frame["y"], frame["zoom"]) == (0.0, 0.3, 0.6, 1.2)
    assert fit["layout"] == "fit" and fit["regions"][0]["keyframes"][0]["zoom"] == 1.1


def test_fill_x_y_become_window_centres_when_source_size_known():
    sources = {"a": {"duration": 300.0, "width": 1920, "height": 1080}}
    timeline = {"schema_version": 2, "clips": [
        {"id": "c1", "source_id": "a", "start": 0, "end": 5, "framing": {"mode": "fill", "x": 0.0, "y": 0.5, "zoom": 1.0}},
        {"id": "c2", "source_id": "a", "start": 5, "end": 10, "framing": {"mode": "fill", "x": 1.0, "y": 0.5, "zoom": 1.0}}]}
    left, right = (s["regions"][0]["keyframes"][0] for s in tl.from_v2(timeline, sources)["shots"])
    window = (9 / 16) / (16 / 9)  # window width as a fraction of the 16:9 source
    assert left["x"] == pytest.approx(window / 2)  # v2 x=0 = window flush left
    assert right["x"] == pytest.approx(1 - window / 2)


def test_canvas_style_and_aspect():
    assert tl.from_v2(v2(canvas_style="blurred"), SOURCES)["canvas"]["background"]["kind"] == "blur"
    assert tl.from_v2(v2(canvas_style="plain"), SOURCES)["canvas"]["background"] == {"kind": "color", "color": "#000000"}
    wide = tl.from_v2(v2(aspect_ratio="16:9", width=1280, height=720, fps=25), SOURCES)["canvas"]
    assert (wide["width"], wide["height"], wide["fps"]) == (1280, 720, 25)


@pytest.mark.parametrize("v2_style,template", [("clean_focus", "boxed_word"), ("editorial", "color_word"), ("phrase", "phrase_clean")])
def test_caption_style_mapping(v2_style, template):
    assert tl.from_v2(v2(caption_style=v2_style), SOURCES)["captions"]["template_id"] == template


def test_captions_false_and_normalize():
    out = tl.from_v2(v2(captions=False, audio={"normalize": False}), SOURCES)
    assert out["captions"]["enabled"] is False and out["audio"]["normalize"] is False


def test_titles_become_hooks_and_broll_maps_fields():
    out = tl.from_v2(v2(titles=[{"text": " Why it matters ", "at": 1, "duration": 3}, {"text": "x" * 200, "at": 5, "duration": 2}],
                        broll=[{"id": "b1", "source_id": "b", "start": 4, "end": 7, "at": 8,
                                "framing": {"mode": "fill", "x": 0.4, "y": 0.5, "zoom": 1.3}}]), SOURCES)
    hooks = out["overlays"]
    assert [(h["kind"], h["start"], h["end"], h["style"]) for h in hooks] == [("hook", 1.0, 4.0, "hook_box"), ("hook", 5.0, 7.0, "hook_box")]
    assert hooks[0]["text"] == "Why it matters" and len(hooks[1]["text"]) == 120
    broll = out["broll"][0]
    assert (broll["id"], broll["source_id"], broll["source_start"], broll["at"], broll["duration"]) == ("b1", "b", 4.0, 8.0, 3.0)
    assert broll["framing"]["zoom"] == 1.3


def test_v1_timeline_converts_and_v3_output_is_valid_idempotent():
    v1 = {"clips": [{"id": "c1", "source_id": "a", "start": 0, "end": 5, "role": "hook"},
                    {"id": "c2", "source_id": "a", "start": 5, "end": 10, "role": "payoff"}]}
    out = tl.from_v2(v1, SOURCES)
    assert out["canvas"]["width"] == 1280 and out["canvas"]["height"] == 720
    assert tl.validate(out, SOURCES) == out


def test_invalid_v2_raises_timeline_error_and_long_ids_replaced():
    with pytest.raises(tl.TimelineError):
        tl.from_v2({"schema_version": 2, "clips": []}, SOURCES)
    out = tl.from_v2(v2(clips=[{"id": "c" * 80, "source_id": "a", "start": 0, "end": 5}]), SOURCES)
    assert out["segments"][0]["id"] == "s1"


def test_program_words_follow_converted_clips():
    out = tl.from_v2(v2(), SOURCES)
    words = tl.program_words(out, SOURCES)
    assert words and words[0]["key"] == "a:0" and words[0]["start"] == pytest.approx(0.0)
    assert planner.validate_timeline(v2(), SOURCES)["duration"] == tl.duration(out)
