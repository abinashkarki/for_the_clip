"""Timeline v3 contract rules: canvas, segments, cuts, shots, b-roll, captions, overlays."""

from __future__ import annotations

import copy

import pytest

from clippie import timeline_v3 as tl


def words(source: str, start: float, count: int, step: float = 0.5) -> list[dict]:
    return [{"id": i, "text": f"w{i}", "start": start + i * step, "end": start + i * step + step * 0.8}
            for i in range(count)]


SOURCES = {"a": {"duration": 100.0, "words": words("a", 0.0, 120)}, "b": {"duration": 40.0, "words": words("b", 0.0, 40)},
           "pic": {"duration": 20.0}}


def base() -> dict:
    return {"schema_version": 3, "segments": [{"id": "s1", "source_id": "a", "start": 10.0, "end": 30.0, "cuts": []}]}


def fails(mutate, path: str, timeline: dict | None = None):
    data = copy.deepcopy(timeline or base())
    mutate(data)
    with pytest.raises(tl.TimelineError) as err:
        tl.validate(data, SOURCES)
    assert err.value.path == path, (err.value.path, str(err.value))


def valid(mutate=None) -> dict:
    data = base()
    if mutate:
        mutate(data)
    return tl.validate(data, SOURCES)


# ------------------------------------------------------------------ canonical form

def test_minimal_timeline_canonical_defaults():
    out = valid()
    assert out["canvas"] == {"width": 1080, "height": 1920, "fps": 30, "background": {"kind": "blur", "color": "#000000"}}
    assert out["shots"] == [{"id": "shot1", "start": 0.0, "end": 20.0, "layout": "fit",
                             "regions": [{"keyframes": [{"t": 0.0, "x": 0.5, "y": 0.5, "zoom": 1.0}]}]}]
    assert out["captions"] == {"enabled": True, "template_id": "bold_pop", "overrides": {}, "emphasis": [], "hidden": []}
    assert out["audio"] == {"normalize": True} and out["broll"] == [] and out["overlays"] == [] and out["brand_kit_id"] is None
    assert tl.validate(out, SOURCES) == out  # idempotent
    assert tl.duration(out) == pytest.approx(20.0)


def test_input_not_mutated_and_unknown_top_level_dropped():
    data = base()
    data["junk"] = 1
    snapshot = copy.deepcopy(data)
    out = tl.validate(data, SOURCES)
    assert data == snapshot and "junk" not in out


@pytest.mark.parametrize("value", [None, 2, "3", True])
def test_schema_version_must_be_3(value):
    fails(lambda t: t.update(schema_version=value), "schema_version")


def test_non_object_inputs():
    with pytest.raises(tl.TimelineError):
        tl.validate([], SOURCES)
    with pytest.raises(tl.TimelineError):
        tl.validate(base(), [])


# ------------------------------------------------------------------ canvas

@pytest.mark.parametrize("w,h", [(1080, 1920), (1920, 1080), (1080, 1080), (1080, 1350), (360, 640), (240, 240)])
def test_canvas_accepts_allowed_aspects(w, h):
    out = valid(lambda t: t.update(canvas={"width": w, "height": h, "fps": 24}))
    assert (out["canvas"]["width"], out["canvas"]["height"], out["canvas"]["fps"]) == (w, h, 24)


@pytest.mark.parametrize("canvas,path", [
    ({"width": 1080, "height": 1921}, "canvas.height"),
    ({"width": 1081, "height": 1920}, "canvas.width"),
    ({"width": 238, "height": 424}, "canvas.width"),
    ({"width": 3842, "height": 6830}, "canvas.width"),
    ({"width": 1000, "height": 1920}, "canvas"),
    ({"width": 1920, "height": 1200}, "canvas"),
    ({"width": 1080.0, "height": 1920}, "canvas.width"),
    ({"fps": 60}, "canvas.fps"),
    ({"fps": True}, "canvas.fps"),
    ({"background": {"kind": "gradient"}}, "canvas.background.kind"),
    ({"background": {"kind": "color", "color": "red"}}, "canvas.background.color"),
])
def test_canvas_rejects(canvas, path):
    fails(lambda t: t.update(canvas=canvas), path)


def test_aspect_of():
    assert tl.aspect_of({"width": 1080, "height": 1350}) == "4:5"


# ------------------------------------------------------------------ segments

def test_segments_required_and_bounds():
    fails(lambda t: t.update(segments=[]), "segments")
    fails(lambda t: t.pop("segments"), "segments")
    fails(lambda t: t["segments"][0].update(source_id="zzz"), "segments[0].source_id")
    fails(lambda t: t["segments"][0].update(start=-1), "segments[0]")
    fails(lambda t: t["segments"][0].update(end=100.5), "segments[0]")
    fails(lambda t: t["segments"][0].update(start=30, end=30), "segments[0]")
    fails(lambda t: t["segments"][0].update(start="x"), "segments[0].start")
    fails(lambda t: t["segments"][0].update(id=""), "segments[0].id")
    fails(lambda t: t["segments"][0].update(id="x" * 65), "segments[0].id")
    valid(lambda t: t["segments"][0].update(start=0, end=100))  # exact bounds allowed, 100 s <= 180


def test_segment_ids_unique_and_overlap_rules():
    def add(t, start, end, sid="s2", source="a"):
        t["segments"].append({"id": sid, "source_id": source, "start": start, "end": end})
    fails(lambda t: add(t, 40, 45, sid="s1"), "segments[1].id")
    fails(lambda t: add(t, 25, 45), "segments[1]")  # overlaps 10-30 of same source
    valid(lambda t: add(t, 30, 45))  # touching allowed
    valid(lambda t: add(t, 0, 10))
    valid(lambda t: add(t, 10, 20, source="b"))  # different source may overlap in numbers
    out = valid(lambda t: add(t, 5, 8, source="pic"))
    assert out["segments"][1]["source_id"] == "pic"


def test_cuts_rules():
    def cuts(*items):
        return lambda t: t["segments"][0].update(cuts=[{"start": a, "end": b, "reason": r} for a, b, r in items])
    out = valid(cuts((12, 12.5, "filler"), (20, 20.04, "silence"), (25, 26, "manual")))
    assert len(out["segments"][0]["cuts"]) == 3
    assert tl.duration(out) == pytest.approx(20 - 0.5 - 0.04 - 1)
    fails(cuts((12, 12.03, "filler")), "segments[0].cuts[0]")  # < 0.04 s
    fails(cuts((10, 11, "filler")), "segments[0].cuts[0]")  # touches segment start: not strictly inside
    fails(cuts((29, 30, "filler")), "segments[0].cuts[0]")
    fails(cuts((9, 11, "filler")), "segments[0].cuts[0]")
    fails(cuts((20, 21, "filler"), (12, 13, "filler")), "segments[0].cuts[1]")  # unsorted
    fails(cuts((12, 14, "filler"), (13, 15, "filler")), "segments[0].cuts[1]")  # overlap
    fails(cuts((12, 13, "boredom")), "segments[0].cuts[0].reason")
    fails(lambda t: t["segments"][0].update(cuts={}), "segments[0].cuts")
    fails(lambda t: t["segments"][0].update(cuts=[5]), "segments[0].cuts[0]")
    fails(cuts((12, "a", "filler")), "segments[0].cuts[0].end")
    valid(cuts((12, 13, "filler"), (13, 14, "filler")))  # adjacent cuts are fine


def test_program_duration_limits():
    fails(lambda t: t["segments"][0].update(start=10, end=10.4), "segments")
    fails(lambda t: t["segments"][0].update(start=10, end=10.5), "segments")  # must be > 0.5
    valid(lambda t: t["segments"][0].update(start=10, end=10.6))
    big = {"a": {"duration": 400.0}}
    ok = {"schema_version": 3, "segments": [{"id": "s", "source_id": "a", "start": 0, "end": 180}]}
    tl.validate(ok, big)
    ok["segments"][0]["end"] = 180.5
    with pytest.raises(tl.TimelineError) as err:
        tl.validate(ok, big)
    assert err.value.path == "segments"


def test_program_map_and_duration():
    out = valid(lambda t: (t["segments"][0].update(cuts=[{"start": 15, "end": 16, "reason": "filler"}]),
                           t["segments"].append({"id": "s2", "source_id": "b", "start": 2, "end": 5})))
    assert tl.program_map(out) == [
        {"source_id": "a", "src_start": 10.0, "src_end": 15.0, "at": 0.0},
        {"source_id": "a", "src_start": 16.0, "src_end": 30.0, "at": 5.0},
        {"source_id": "b", "src_start": 2.0, "src_end": 5.0, "at": 19.0}]
    assert tl.duration(out) == pytest.approx(22.0)


# ------------------------------------------------------------------ shots

def shot(sid, a, b, layout="fill", regions=None):
    out = {"id": sid, "start": a, "end": b, "layout": layout}
    if regions is not None:
        out["regions"] = regions
    return out


def region(*frames):
    return {"keyframes": [dict(zip(("t", "x", "y", "zoom"), f)) for f in frames]}


def test_shots_valid_forms():
    out = valid(lambda t: t.update(shots=[
        shot("a", 0, 5, "fill", [region((0, 0.4, 0.5, 1.0), (5, 0.6, 0.5, 1.5))]),
        shot("b", 5, 12, "stack", [region((5, 0.3, 0.4, 1)), region((9, 0.7, 0.6, 2))]),
        shot("c", 12, 16, "screen_speaker", [region((12, 0.5, 0.5, 1.2)), region((12, 0.5, 0.3, 1))]),
        shot("d", 16, 20.0004, "fit")]))
    assert [s["layout"] for s in out["shots"]] == ["fill", "stack", "screen_speaker", "fit"]
    assert out["shots"][-1]["end"] == 20.0  # snapped to program duration
    assert out["shots"][3]["regions"][0]["keyframes"][0] == {"t": 16.0, "x": 0.5, "y": 0.5, "zoom": 1.0}  # defaulted


def test_shots_contiguity_and_snap():
    fails(lambda t: t.update(shots=[shot("a", 0.5, 20)]), "shots[0].start")
    fails(lambda t: t.update(shots=[shot("a", 0, 10), shot("b", 11, 20)]), "shots[1].start")
    fails(lambda t: t.update(shots=[shot("a", 0, 10), shot("b", 9, 20)]), "shots[1].start")
    fails(lambda t: t.update(shots=[shot("a", 0, 10), shot("b", 10, 19)]), "shots[1].end")
    fails(lambda t: t.update(shots=[shot("a", 0, 21)]), "shots[0].end")
    fails(lambda t: t.update(shots=[shot("a", 0, 10), shot("b", 10, 20), shot("c", 20, 20)]), "shots[2]")
    out = valid(lambda t: t.update(shots=[shot("a", 0.0005, 10.0004), shot("b", 10.0, 20.0008)]))
    assert out["shots"][0]["start"] == 0.0 and out["shots"][1]["start"] == out["shots"][0]["end"]


def test_shot_layout_and_region_rules():
    fails(lambda t: t.update(shots=[shot("a", 0, 20, "grid")]), "shots[0].layout")
    fails(lambda t: t.update(shots=[shot("a", 0, 20, "fill", [])]), "shots[0].regions")
    fails(lambda t: t.update(shots=[shot("a", 0, 20, "stack", [region((0, .5, .5, 1))])]), "shots[0].regions")
    fails(lambda t: t.update(shots=[shot("a", 0, 20, "fill", [region((0, .5, .5, 1)), region((0, .5, .5, 1))])]), "shots[0].regions")
    fails(lambda t: t.update(shots=[shot("a", 0, 20, "fill", [{"keyframes": []}])]), "shots[0].regions[0].keyframes")
    fails(lambda t: t.update(shots=[shot("a", 0, 20, "fill", [{"source_id": "b", "keyframes": [{"t": 0}]}])]),
          "shots[0].regions[0].source_id")
    fails(lambda t: t.update(shots=[shot("a", 0, 20), shot("a", 20, 20.5)]), "shots[1].id")


@pytest.mark.parametrize("frames,path", [
    ([(0, 1.1, .5, 1)], "shots[0].regions[0].keyframes[0].x"),
    ([(0, .5, -.1, 1)], "shots[0].regions[0].keyframes[0].y"),
    ([(0, .5, .5, 0.9)], "shots[0].regions[0].keyframes[0].zoom"),
    ([(0, .5, .5, 3.1)], "shots[0].regions[0].keyframes[0].zoom"),
    ([(-1, .5, .5, 1)], "shots[0].regions[0].keyframes[0].t"),
    ([(21, .5, .5, 1)], "shots[0].regions[0].keyframes[0].t"),
    ([(0, .5, .5, 1), (0, .5, .5, 1)], "shots[0].regions[0].keyframes[1].t"),
    ([(5, .5, .5, 1), (3, .5, .5, 1)], "shots[0].regions[0].keyframes[1].t"),
    ([(0, float("nan"), .5, 1)], "shots[0].regions[0].keyframes[0].x"),
])
def test_keyframe_rules(frames, path):
    fails(lambda t: t.update(shots=[shot("a", 0, 20, "fill", [region(*frames)])]), path)


def test_interpolate_linear_hold_and_clamp():
    frames = [{"t": 2.0, "x": 0.2, "y": 0.4, "zoom": 1.0}, {"t": 4.0, "x": 0.6, "y": 0.8, "zoom": 2.0}]
    assert tl.interpolate(frames, 0.0) == {"x": 0.2, "y": 0.4, "zoom": 1.0}
    assert tl.interpolate(frames, 3.0) == pytest.approx({"x": 0.4, "y": 0.6, "zoom": 1.5})
    assert tl.interpolate(frames, 9.0) == {"x": 0.6, "y": 0.8, "zoom": 2.0}
    assert tl.interpolate(frames[:1], 7.0)["zoom"] == 1.0


# ------------------------------------------------------------------ b-roll

def broll(bid="b1", source="pic", source_start=2.0, at=3.0, duration=2.5, **extra):
    return {"id": bid, "source_id": source, "source_start": source_start, "at": at, "duration": duration, **extra}


def test_broll_valid_and_defaults():
    out = valid(lambda t: t.update(broll=[broll(), broll("b2", at=8, framing={"x": 0.3, "zoom": 2})]))
    assert out["broll"][0]["framing"] == {"x": 0.5, "y": 0.5, "zoom": 1.0}
    assert out["broll"][1]["framing"] == {"x": 0.3, "y": 0.5, "zoom": 2.0}


@pytest.mark.parametrize("items,path", [
    ([broll(source="zzz")], "broll[0].source_id"),
    ([broll(duration=0.2)], "broll[0].duration"),
    ([broll(source_start=18.0, duration=3.0)], "broll[0]"),
    ([broll(at=18.0, duration=3.0)], "broll[0]"),
    ([broll(at=-1)], "broll[0].at"),
    ([broll(), broll("b2", at=4.0)], "broll[1]"),
    ([broll(at=8), broll("b2", at=3.0)], "broll[1]"),
    ([broll(), broll()], "broll[1].id"),
    ([broll(framing={"zoom": 4})], "broll[0].framing.zoom"),
    ([broll(framing={"x": 2})], "broll[0].framing.x"),
])
def test_broll_rules(items, path):
    fails(lambda t: t.update(broll=items), path)


def test_broll_adjacent_allowed_and_picture_source_ok():
    valid(lambda t: t.update(broll=[broll(at=3.0, duration=2.0), broll("b2", at=5.0, duration=0.3)]))


# ------------------------------------------------------------------ captions

def test_caption_defaults_and_template_validation():
    fails(lambda t: t.update(captions={"template_id": "nope"}), "captions.template_id")
    fails(lambda t: t.update(captions={"enabled": "yes"}), "captions.enabled")
    fails(lambda t: t.update(captions=[]), "captions")
    out = valid(lambda t: t.update(captions={"template_id": "karaoke", "enabled": False}))
    assert out["captions"]["template_id"] == "karaoke" and out["captions"]["enabled"] is False


def test_caption_overrides_whitelist():
    out = valid(lambda t: t.update(captions={"overrides": {"colors": {"active": "#00FF00"}, "case": "lower"}}))
    assert out["captions"]["overrides"]["colors"]["active"] == "#00FF00"
    fails(lambda t: t.update(captions={"overrides": {"grouping": {"max_lines": 3}}}), "captions.overrides.grouping.max_lines")
    fails(lambda t: t.update(captions={"overrides": {"line_height": 2}}), "captions.overrides.line_height")
    fails(lambda t: t.update(captions={"overrides": {"colors": {"text": "bad"}}}), "captions.overrides.colors.text")
    fails(lambda t: t.update(captions={"overrides": {"font": {"family": "X"}}}), "captions.overrides.font")
    fails(lambda t: t.update(captions={"overrides": "x"}), "captions.overrides")


def test_caption_keys_dropped_when_unknown_and_deduped():
    out = valid(lambda t: t.update(captions={"emphasis": ["a:3", "a:3", "a:9999", "zzz:1", 5, "a:100"], "hidden": ["b:2", "bogus"]}))
    assert out["captions"]["emphasis"] == ["a:3", "a:100"]  # a:100 exists but may be outside the kept ranges
    assert out["captions"]["hidden"] == ["b:2"]
    fails(lambda t: t.update(captions={"emphasis": "a:3"}), "captions.emphasis")


def test_program_words_clipping_mapping_and_flags():
    # words: id i spans [0.5i, 0.5i + 0.4]; segment 10.1..12.3 with a cut 11.0..11.5
    out = valid(lambda t: (t["segments"][0].update(start=10.1, end=12.3, cuts=[{"start": 11.0, "end": 11.5, "reason": "filler"}]),
                           t.update(captions={"emphasis": ["a:21"], "hidden": ["a:24"]})))
    by_key = {w["key"]: w for w in tl.program_words(out, SOURCES)}
    assert by_key["a:20"]["start"] == pytest.approx(0.0) and by_key["a:20"]["end"] == pytest.approx(0.3)  # clipped to 10.1
    assert by_key["a:21"]["emphasis"] is True and by_key["a:21"]["hidden"] is False
    assert by_key["a:24"]["hidden"] is True  # returned but flagged; kept range 2 starts at program 0.9 + ...
    assert by_key["a:24"]["start"] == pytest.approx(0.9 + 0.5)
    assert "a:22" not in by_key  # 11.0-11.4 lies in the cut


def test_program_words_excludes_cut_words_and_maps_after_cut():
    out = valid(lambda t: t["segments"][0].update(start=10.0, end=14.0, cuts=[{"start": 11.0, "end": 12.0, "reason": "filler"}]))
    pw = tl.program_words(out, SOURCES)
    keys = [w["key"] for w in pw]
    assert "a:22" not in keys and "a:23" not in keys  # words 11.0-11.4 and 11.5-11.9 are cut
    assert "a:21" in keys and "a:24" in keys
    after = next(w for w in pw if w["key"] == "a:24")  # starts 12.0 -> program 1.0 + (12-12)... = 1.0 + 0
    assert after["start"] == pytest.approx(1.0) and after["source_id"] == "a" and after["word_id"] == 24
    assert all(a["start"] <= b["start"] for a, b in zip(pw, pw[1:]))


def test_program_words_midpoint_rule_at_range_edges():
    # word a:20 spans 10.0-10.4 (mid 10.2); range starting 10.3 excludes it, range starting 10.1 includes it
    for start, expected in ((10.3, False), (10.1, True)):
        out = valid(lambda t: t["segments"][0].update(start=start, end=14.0))
        assert ("a:20" in [w["key"] for w in tl.program_words(out, SOURCES)]) is expected
    out = valid(lambda t: t["segments"][0].update(start=10.0, end=10.3 + 0.5))
    last = tl.program_words(out, SOURCES)
    assert max(w["end"] for w in last) <= tl.duration(out) + 1e-9  # clipped to the range


def test_program_words_across_segments_and_sources():
    out = valid(lambda t: t["segments"].append({"id": "s2", "source_id": "b", "start": 1.0, "end": 3.0}))
    pw = tl.program_words(out, SOURCES)
    b_words = [w for w in pw if w["source_id"] == "b"]
    assert b_words and b_words[0]["start"] >= 20.0 - 1e-9
    assert {w["key"] for w in b_words} == {"b:2", "b:3", "b:4", "b:5"}


# ------------------------------------------------------------------ overlays

def hook(oid="o1", start=0.0, end=3.0, text="He was wrong", **extra):
    return {"id": oid, "kind": "hook", "text": text, "start": start, "end": end, **extra}


def test_overlay_valid_kinds_and_defaults():
    out = valid(lambda t: t.update(overlays=[
        hook(style="hook_banner"), {"id": "o2", "kind": "progress_bar"},
        {"id": "o3", "kind": "logo", "asset_id": "brand-1"}]))
    assert out["overlays"][0]["style"] == "hook_banner"
    assert out["overlays"][1] == {"id": "o2", "kind": "progress_bar", "color": "#FFD400", "position": "bottom", "thickness": 12.0}
    assert out["overlays"][2]["anchor"] == "top_right" and out["overlays"][2]["opacity"] == 0.9


@pytest.mark.parametrize("items,path", [
    ([hook(text="")], "overlays[0].text"),
    ([hook(text="x" * 121)], "overlays[0].text"),
    ([hook(start=3, end=3)], "overlays[0]"),
    ([hook(end=20.5)], "overlays[0]"),
    ([hook(start=-1)], "overlays[0].start"),
    ([hook(style="nope")], "overlays[0].style"),
    ([hook(), hook("o2", 2.5, 4)], "overlays[1]"),
    ([hook(), hook()], "overlays[1].id"),
    ([{"id": "p", "kind": "progress_bar"}, {"id": "q", "kind": "progress_bar"}], "overlays[1]"),
    ([{"id": "p", "kind": "progress_bar", "color": "yellow"}], "overlays[0].color"),
    ([{"id": "p", "kind": "progress_bar", "position": "middle"}], "overlays[0].position"),
    ([{"id": "p", "kind": "progress_bar", "thickness": 3}], "overlays[0].thickness"),
    ([{"id": "p", "kind": "progress_bar", "thickness": 41}], "overlays[0].thickness"),
    ([{"id": "l", "kind": "logo"}], "overlays[0].asset_id"),
    ([{"id": "l", "kind": "logo", "asset_id": "x" * 101}], "overlays[0].asset_id"),
    ([{"id": "l", "kind": "logo", "asset_id": "a", "anchor": "center"}], "overlays[0].anchor"),
    ([{"id": "l", "kind": "logo", "asset_id": "a", "scale": 0.04}], "overlays[0].scale"),
    ([{"id": "l", "kind": "logo", "asset_id": "a", "scale": 0.41}], "overlays[0].scale"),
    ([{"id": "l", "kind": "logo", "asset_id": "a", "opacity": 0.05}], "overlays[0].opacity"),
    ([{"id": "x", "kind": "sticker"}], "overlays[0].kind"),
])
def test_overlay_rules(items, path):
    fails(lambda t: t.update(overlays=items), path)


def test_hooks_adjacent_allowed_and_text_trimmed():
    out = valid(lambda t: t.update(overlays=[hook(text="  hi  "), hook("o2", 3.0, 5.0)]))
    assert out["overlays"][0]["text"] == "hi"


def test_misc_fields():
    out = valid(lambda t: t.update(brand_kit_id="kit9", audio={"normalize": False}))
    assert out["brand_kit_id"] == "kit9" and out["audio"] == {"normalize": False}
    fails(lambda t: t.update(brand_kit_id=5), "brand_kit_id")
    fails(lambda t: t.update(audio={"normalize": "yes"}), "audio.normalize")


# ------------------------------------------------------------------ default()

def test_default_builds_valid_timeline_per_aspect():
    for aspect, size in (("9:16", (1080, 1920)), ("16:9", (1920, 1080)), ("1:1", (1080, 1080)), ("4:5", (1080, 1350))):
        out = tl.default("a", 5.0, 25.0, SOURCES, aspect=aspect)
        assert (out["canvas"]["width"], out["canvas"]["height"]) == size
        assert tl.duration(out) == pytest.approx(20.0)
    with pytest.raises(tl.TimelineError):
        tl.default("a", 5.0, 25.0, SOURCES, aspect="2:1")
    with pytest.raises(tl.TimelineError):
        tl.default("a", 25.0, 5.0, SOURCES)
