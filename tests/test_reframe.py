"""Reframe logic on injected analysis (no model, no media). Integration with timeline_v3.validate
runs when stream B's module is importable; otherwise `check_shots` enforces the contract rules locally."""
import numpy as np
import pytest

from clippie import reframe
from clippie.analyzers import AnalyzerUnavailable

SW, SH = 1280, 720
ASPECT = SW / SH
CANVAS = 9 / 16
FPS = 4.0
CHECK_LAYOUT_REGIONS = {"fill": 1, "fit": 1, "stack": 2, "screen_speaker": 2}


def face(cx, cy, w=0.07, h=0.16):
    return (cx - w / 2, cy - h / 2, w, h)


def track(tid, box_at, t0, t1, fps=FPS):
    n = int(round((t1 - t0) * fps))
    samples = []
    for i in range(n):
        x, y, w, h = box_at(t0 + i / fps)
        samples.append({"t": round(t0 + i / fps, 3), "x": round(x, 4), "y": round(y, 4), "w": w, "h": h, "score": 0.9})
    return {"id": tid, "samples": samples}


def static(box):
    return lambda t: box


def faces_doc(*tracks):
    return {"version": 1, "sample_fps": FPS, "width": SW, "height": SH, "tracks": list(tracks)}


def timeline(segments, canvas=(1080, 1920)):
    return {"schema_version": 3, "canvas": {"width": canvas[0], "height": canvas[1], "fps": 30},
            "segments": [{"id": f"s{i}", "source_id": sid, "start": a, "end": b, "cuts": cuts}
                         for i, (sid, a, b, cuts) in enumerate(segments)]}


def one(start=0.0, end=20.0, cuts=(), sid="a"):
    return timeline([(sid, start, end, list(cuts))])


SOURCES = {"a": {"source_path": "/nonexistent/a.mp4", "duration": 600.0, "width": SW, "height": SH},
           "b": {"source_path": "/nonexistent/b.mp4", "duration": 600.0, "width": SW, "height": SH}}


def run(tl, faces, scenes=(), speech=None, sid="a"):
    return reframe.auto_shots(tl, SOURCES, analysis={sid: {"faces": faces, "scenes": list(scenes), "speech": speech}})


def duration(tl):
    return sum(r["src_end"] - r["src_start"] for r in reframe.kept_ranges(tl))


def check_shots(shots, dur, region_aspects=None):
    """The shot rules of TIMELINE_V3.md that reframe relies on."""
    assert shots and shots[0]["start"] == 0
    assert abs(shots[-1]["end"] - dur) <= 0.001
    ids = set()
    for a, b in zip(shots, shots[1:]):
        assert abs(a["end"] - b["start"]) <= 0.001
    for s in shots:
        assert s["id"] not in ids and s["start"] < s["end"]
        ids.add(s["id"])
        assert len(s["regions"]) == CHECK_LAYOUT_REGIONS[s["layout"]]
        for region in s["regions"]:
            kfs = region["keyframes"]
            assert kfs
            ts = [k["t"] for k in kfs]
            assert all(a < b for a, b in zip(ts, ts[1:]))
            assert s["start"] - 1e-9 <= ts[0] and ts[-1] < s["end"]
            assert all(0 <= k["x"] <= 1 and 0 <= k["y"] <= 1 and 1 <= k["zoom"] <= 3 for k in kfs)
            assert all(b - a >= 0.5 - 1e-6 for a, b in zip(ts, ts[1:]))  # at most 2 keyframes / s
        assert "source_id" not in s["regions"][0]


def window(shot, region, t, aspect):
    kfs = shot["regions"][region]["keyframes"]
    ww, wh = reframe.window_size(aspect, ASPECT, kfs[0]["zoom"])
    x, y = reframe.clamp_centre(*reframe.interpolate(kfs, t), ww, wh)
    return x - ww / 2, y - wh / 2, ww, wh


def assert_face_inside(shot, region, tr, aspect, t0=None, t1=None):
    for s in tr["samples"]:
        if not (shot["start"] <= s["t"] < shot["end"]):
            continue
        wx, wy, ww, wh = window(shot, region, s["t"], aspect)
        assert s["x"] >= wx - 0.005 and s["x"] + s["w"] <= wx + ww + 0.005, s
        assert s["y"] >= wy - 0.005 and s["y"] + s["h"] <= wy + wh + 0.005, s


# ---- program map -----------------------------------------------------------------------

def test_kept_ranges_remove_cuts_and_accumulate_program_time():
    tl = timeline([("a", 10, 20, [{"start": 12, "end": 13, "reason": "filler"}]), ("b", 5, 8, [])])
    assert reframe.kept_ranges(tl) == [
        {"source_id": "a", "src_start": 10.0, "src_end": 12.0, "at": 0.0},
        {"source_id": "a", "src_start": 13.0, "src_end": 20.0, "at": 2.0},
        {"source_id": "b", "src_start": 5.0, "src_end": 8.0, "at": 9.0}]


def test_local_program_map_matches_timeline_v3_when_available():
    tv3 = pytest.importorskip("clippie.timeline_v3")
    tl = timeline([("a", 10, 20, [{"start": 12, "end": 13, "reason": "filler"}]), ("b", 5, 8, [])])
    assert tv3.program_map(tl) == reframe.kept_ranges(tl)


# ---- layout decisions ------------------------------------------------------------------

def test_no_faces_is_one_fit_shot_covering_the_clip():
    tl = one(5, 25)
    shots = run(tl, faces_doc())
    check_shots(shots, 20)
    assert [s["layout"] for s in shots] == ["fit"] and len(shots) == 1


def test_single_static_face_is_fill_with_one_keyframe_in_the_upper_middle():
    tr = track("f1", static(face(0.6, 0.45)), 0, 20)
    shots = run(one(), faces_doc(tr))
    check_shots(shots, 20)
    (shot,) = shots
    assert shot["layout"] == "fill" and len(shot["regions"][0]["keyframes"]) == 1
    assert_face_inside(shot, 0, tr, CANVAS)
    wx, wy, ww, wh = window(shot, 0, 5, CANVAS)
    centre_y = (0.45 - wy) / wh
    assert 0.25 <= centre_y <= 0.5
    assert (0.6 - wx) / ww == pytest.approx(0.5, abs=0.02)  # centred horizontally


def test_face_is_never_cropped_even_at_the_frame_edge():
    tr = track("f1", static(face(0.96, 0.4, 0.06, 0.14)), 0, 10)
    shots = run(one(0, 10), faces_doc(tr))
    check_shots(shots, 10)
    assert shots[0]["layout"] == "fill"
    assert_face_inside(shots[0], 0, tr, CANVAS)


def test_face_wider_than_the_window_falls_back_to_fit():
    big = track("f1", static((0.3, 0.1, 0.4, 0.8)), 0, 10)
    assert [s["layout"] for s in run(one(0, 10), faces_doc(big))] == ["fit"]


def test_tiny_background_faces_are_ignored():
    tiny = track("f1", static(face(0.5, 0.5, 0.02, 0.03)), 0, 10)
    assert [s["layout"] for s in run(one(0, 10), faces_doc(tiny))] == ["fit"]


def test_face_present_only_briefly_does_not_drive_the_framing():
    blip = track("f1", static(face(0.5, 0.45)), 0, 3)
    assert [s["layout"] for s in run(one(0, 20), faces_doc(blip))] == ["fit"]


def test_moving_face_is_followed_within_keyframe_and_speed_limits():
    tr = track("f1", lambda t: face(0.25 + 0.5 * t / 20, 0.45), 0, 20)
    (shot,) = run(one(), faces_doc(tr))
    check_shots([shot], 20)
    kfs = shot["regions"][0]["keyframes"]
    assert len(kfs) > 4
    ww, _ = reframe.window_size(CANVAS, ASPECT, kfs[0]["zoom"])
    for a, b in zip(kfs, kfs[1:]):
        assert abs(b["x"] - a["x"]) / ww / (b["t"] - a["t"]) <= reframe.PLANS[-1][1] + 1e-6
    assert kfs[-1]["x"] > kfs[0]["x"] + 0.3
    assert_face_inside(shot, 0, tr, CANVAS)


def test_small_jitter_inside_the_dead_zone_makes_no_keyframes():
    rng = np.random.default_rng(3)
    jitter = {round(i / FPS, 3): rng.normal(0, 0.002) for i in range(200)}
    tr = track("f1", lambda t: face(0.5 + jitter[round(t, 3)], 0.45), 0, 20)
    (shot,) = run(one(), faces_doc(tr))
    assert len(shot["regions"][0]["keyframes"]) == 1


def test_two_visible_faces_are_stacked_left_face_on_top():
    left, right = track("f1", static(face(0.25, 0.45)), 0, 20), track("f2", static(face(0.75, 0.45)), 0, 20)
    shots = run(one(), faces_doc(right, left))
    check_shots(shots, 20)
    (shot,) = shots
    assert shot["layout"] == "stack"
    half = 1080 / 960
    assert_face_inside(shot, 0, left, half) and assert_face_inside(shot, 1, right, half)
    assert shot["regions"][0]["keyframes"][0]["x"] < shot["regions"][1]["keyframes"][0]["x"]


def speech_for(faces, active):
    """active: {track_id: lambda t -> 0..1}"""
    return {"version": 1, "audio": True, "per_track": {
        t["id"]: [{"t": s["t"], "active": active[t["id"]](s["t"])} for s in t["samples"]] for t in faces["tracks"]}}


def test_one_dominant_speaker_gets_fill_instead_of_stack():
    doc = faces_doc(track("f1", static(face(0.25, 0.45)), 0, 20), track("f2", static(face(0.75, 0.45)), 0, 20))
    sp = speech_for(doc, {"f1": lambda t: 0.1, "f2": lambda t: 0.9})
    (shot,) = run(one(), doc, speech=sp)
    assert shot["layout"] == "fill"
    assert shot["regions"][0]["keyframes"][0]["x"] > 0.6


def test_alternating_speakers_stay_stacked():
    doc = faces_doc(track("f1", static(face(0.25, 0.45)), 0, 20), track("f2", static(face(0.75, 0.45)), 0, 20))
    sp = speech_for(doc, {"f1": lambda t: 0.9 if int(t // 4) % 2 == 0 else 0.0, "f2": lambda t: 0.9 if int(t // 4) % 2 else 0.0})
    assert [s["layout"] for s in run(one(), doc, speech=sp)] == ["stack"]


def test_silence_gives_no_speaker_evidence_so_stack():
    doc = faces_doc(track("f1", static(face(0.25, 0.45)), 0, 20), track("f2", static(face(0.75, 0.45)), 0, 20))
    sp = speech_for(doc, {"f1": lambda t: 0.0, "f2": lambda t: 0.0})
    assert [s["layout"] for s in run(one(), doc, speech=sp)] == ["stack"]


def test_second_face_visible_only_part_of_the_time_is_not_stacked():
    doc = faces_doc(track("f1", static(face(0.3, 0.45)), 0, 20), track("f2", static(face(0.8, 0.45)), 0, 11))
    shots = run(one(), doc)
    assert shots[0]["layout"] == "fill"
    assert_face_inside(shots[0], 0, doc["tracks"][0], CANVAS)


def test_three_faces_without_a_dominant_speaker_are_fit():
    doc = faces_doc(*[track(f"f{i}", static(face(x, 0.45)), 0, 20) for i, x in enumerate((0.2, 0.5, 0.8), 1)])
    assert [s["layout"] for s in run(one(), doc)] == ["fit"]


def test_stack_that_cannot_hold_both_faces_falls_back_to_fit():
    doc = faces_doc(track("f1", static((0.0, 0.05, 0.62, 0.9)), 0, 20), track("f2", static((0.38, 0.05, 0.62, 0.9)), 0, 20))
    assert [s["layout"] for s in run(one(), doc)] == ["fit"]


# ---- scene cuts and program mapping -----------------------------------------------------

def test_scene_cut_splits_shots_at_the_mapped_program_time():
    a, b = track("f1", static(face(0.3, 0.45)), 0, 10), track("f2", static(face(0.75, 0.45)), 10, 20)
    tl = one(0, 20, cuts=[{"start": 2.0, "end": 3.0, "reason": "filler"}])
    shots = run(tl, faces_doc(a, b), scenes=[10.0])
    check_shots(shots, 19.0)
    assert [s["layout"] for s in shots] == ["fill", "fill"]
    assert shots[0]["end"] == pytest.approx(9.0) == shots[1]["start"]  # source 10.0 minus the 1 s cut
    assert shots[0]["regions"][0]["keyframes"][0]["x"] < 0.5 < shots[1]["regions"][0]["keyframes"][0]["x"]


def test_cut_without_a_scene_change_does_not_split_the_shot():
    tr = track("f1", static(face(0.5, 0.45)), 0, 20)
    tl = one(0, 20, cuts=[{"start": 5, "end": 5.6, "reason": "filler"}, {"start": 12, "end": 13, "reason": "silence"}])
    shots = run(tl, faces_doc(tr))
    check_shots(shots, duration(tl))
    assert len(shots) == 1


def test_segment_boundaries_always_start_a_new_shot():
    tr = track("f1", static(face(0.5, 0.45)), 0, 40)
    tl = timeline([("a", 0, 10, []), ("a", 20, 30, [])])
    shots = run(tl, faces_doc(tr))
    check_shots(shots, 20)
    assert len(shots) == 2 and shots[0]["end"] == pytest.approx(10.0)


def test_two_sources_use_their_own_analysis():
    tl = timeline([("a", 0, 10, []), ("b", 0, 10, [])])
    analysis = {"a": {"faces": faces_doc(track("f1", static(face(0.3, 0.45)), 0, 10)), "scenes": []},
                "b": {"faces": faces_doc(), "scenes": []}}
    shots = reframe.auto_shots(tl, SOURCES, analysis=analysis)
    check_shots(shots, 20)
    assert [s["layout"] for s in shots] == ["fill", "fit"]


def test_flash_scene_cut_is_absorbed_not_a_sliver_shot():
    tr = track("f1", static(face(0.5, 0.45)), 0, 20)
    shots = run(one(), faces_doc(tr), scenes=[10.0, 10.1, 15.0])
    check_shots(shots, 20)
    assert all(s["end"] - s["start"] >= reframe.MIN_SHOT for s in shots)


def test_adjacent_fit_shots_are_merged():
    shots = run(one(), faces_doc(), scenes=[5.0, 10.0, 15.0])
    assert [s["layout"] for s in shots] == ["fit"] and shots[0]["end"] == 20


def test_cut_times_near_range_edges_are_ignored():
    tr = track("f1", static(face(0.5, 0.45)), 0, 20)
    assert len(run(one(), faces_doc(tr), scenes=[0.01, 19.99])) == 1


# ---- screen_speaker ---------------------------------------------------------------------

def slide_frames(n=6, w=320, h=180, seed=0):
    rng = np.random.default_rng(seed)
    base = np.full((h, w), 235, np.uint8)
    for row in range(10, 150, 14):
        base[row:row + 5, 10:int(rng.integers(100, 240))] = 30
    return [base.copy() for _ in range(n)]


def room_frames(n=6, w=320, h=180):
    return [np.full((h, w), 120, np.uint8) for _ in range(n)]


def corner_doc():
    return faces_doc(track("f1", static(face(0.88, 0.8, 0.07, 0.15)), 0, 10))


def test_looks_like_screen_needs_static_detail():
    box = (0.83, 0.72, 0.07, 0.15)
    assert reframe.looks_like_screen(slide_frames(), box)
    assert not reframe.looks_like_screen(room_frames(), box)  # static but featureless
    rng = np.random.default_rng(1)
    moving = [rng.integers(0, 255, (180, 320)).astype(np.uint8) for _ in range(6)]  # live video, not a slide
    assert not reframe.looks_like_screen(moving, box)
    assert not reframe.looks_like_screen(slide_frames()[:2], box)


def test_small_corner_face_over_a_slide_is_screen_speaker(monkeypatch):
    monkeypatch.setattr(reframe, "_sample_gray", lambda path, a, b: slide_frames())
    shots = run(one(0, 10), corner_doc())
    check_shots(shots, 10)
    (shot,) = shots
    assert shot["layout"] == "screen_speaker"
    assert shot["regions"][0]["keyframes"][0]["zoom"] == 1.0
    assert_face_inside(shot, 1, corner_doc()["tracks"][0], 1080 / (0.4 * 1920))


def test_small_corner_face_over_a_plain_room_is_just_fill(monkeypatch):
    monkeypatch.setattr(reframe, "_sample_gray", lambda path, a, b: room_frames())
    assert [s["layout"] for s in run(one(0, 10), corner_doc())] == ["fill"]


def test_centred_face_never_triggers_screen_speaker(monkeypatch):
    monkeypatch.setattr(reframe, "_sample_gray", lambda *a: pytest.fail("frames sampled for a large central face"))
    run(one(0, 10), faces_doc(track("f1", static(face(0.5, 0.45)), 0, 10)))


# ---- other canvases, determinism, failures ------------------------------------------------

def test_square_canvas_uses_its_own_aspect():
    tr = track("f1", static(face(0.5, 0.45)), 0, 10)
    tl = timeline([("a", 0, 10, [])], canvas=(1080, 1080))
    (shot,) = run(tl, faces_doc(tr))
    assert shot["layout"] == "fill"
    assert_face_inside(shot, 0, tr, 1.0)


def test_portrait_source_allows_vertical_movement():
    doc = {**faces_doc(track("f1", lambda t: face(0.5, 0.25 + 0.5 * t / 20, 0.2, 0.1), 0, 20)), "width": 1080, "height": 1920}
    sources = {"a": {"source_path": "/x", "width": 1080, "height": 1920}}
    (shot,) = reframe.auto_shots(one(), sources, analysis={"a": {"faces": doc, "scenes": []}})
    ys = [k["y"] for k in shot["regions"][0]["keyframes"]]
    assert shot["layout"] in {"fill", "fit"}
    if shot["layout"] == "fill":
        assert ys[-1] > ys[0]


def test_deterministic_and_does_not_mutate_inputs():
    import copy
    tr = track("f1", lambda t: face(0.3 + 0.3 * t / 20, 0.45), 0, 20)
    doc, tl = faces_doc(tr), one()
    before = copy.deepcopy((doc, tl))
    assert run(tl, doc) == run(tl, doc)
    assert (doc, tl) == before


def test_missing_analyzer_dependency_raises_instead_of_guessing(monkeypatch):
    def boom(*a, **k):
        raise AnalyzerUnavailable("no opencv")
    monkeypatch.setattr(reframe.faces_mod, "track", boom)
    monkeypatch.setattr(reframe.scenes_mod, "detect", lambda *a, **k: [])
    with pytest.raises(AnalyzerUnavailable):
        reframe.auto_shots(one(), SOURCES)


def test_computes_missing_analysis_from_the_source_and_caches_per_source(monkeypatch, tmp_path):
    calls = []
    doc = faces_doc(track("f1", static(face(0.5, 0.45)), 0, 20))
    monkeypatch.setattr(reframe.faces_mod, "track", lambda path, start, end, cache_dir: calls.append(("faces", start, end, cache_dir)) or doc)
    monkeypatch.setattr(reframe.scenes_mod, "detect", lambda path, start, end, cache_dir: calls.append(("scenes", start, end, cache_dir)) or [])
    tl = timeline([("a", 0, 20, []), ("a", 200, 210, [])])
    shots = reframe.auto_shots(tl, SOURCES, cache_dir=tmp_path)
    assert [c[:3] for c in calls if c[0] == "faces"] == [("faces", 0, 20), ("faces", 200, 210)]  # far apart: analysed separately
    assert all(c[3] == str(tmp_path / "a") for c in calls)
    assert shots[-1]["end"] == pytest.approx(30)


def test_empty_timeline_is_an_error():
    with pytest.raises(ValueError):
        reframe.auto_shots({"segments": []}, SOURCES, analysis={})


def test_shots_validate_against_timeline_v3_when_available():
    tv3 = pytest.importorskip("clippie.timeline_v3")
    doc = faces_doc(track("f1", lambda t: face(0.3 + 0.3 * t / 20, 0.45), 0, 20))
    tl = one()
    tl["shots"] = run(tl, doc)
    sources = {"a": {"source_path": "x", "duration": 600.0, "words": [], "title": "t"}}
    tv3.validate(tl, sources)
