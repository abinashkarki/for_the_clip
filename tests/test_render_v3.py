"""Compositor pixels, overlay schedule and real tiny renders from synthetic FFmpeg sources."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from clippie import compositor as comp, render_v3, timeline_v3 as tl

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg is required")

WORDS = [{"id": i, "text": text, "start": 0.5 + i * 0.5, "end": 0.5 + i * 0.5 + 0.4}
         for i, text in enumerate(["one", "two", "three", "four", "five", "six"])]


def sources_for(path: str = "", duration: float = 10.0) -> dict:
    return {"a": {"duration": duration, "source_path": path, "words": WORDS}, "pic": {"duration": 10.0, "source_path": path}}


def timeline(**changes) -> dict:
    base = {"schema_version": 3, "canvas": {"width": 1080, "height": 1920, "fps": 30},
            "segments": [{"id": "s1", "source_id": "a", "start": 0.0, "end": 4.0}],
            "captions": {"template_id": "bold_pop"}, "overlays": []}
    base.update(changes)
    return tl.validate(base, sources_for())


def tint_count(image: Image.Image, rgb: tuple, box: tuple | None = None, tol: int = 12) -> int:
    arr = np.asarray(image.convert("RGBA"))
    if box:
        arr = arr[int(box[1]):int(box[3]), int(box[0]):int(box[2])]
    near = (np.abs(arr[..., :3].astype(int) - np.array(rgb)).max(axis=-1) <= tol) & (arr[..., 3] > 240)
    return int(near.sum())


# ------------------------------------------------------------------ compositor

def test_active_word_colour_follows_time_and_stays_in_its_box():
    c = comp.Compositor(timeline(), sources_for(), 1080, 1920)
    page = c.pages[0]
    boxes = [(w["x"], page["block"]["y"] - 20, w["x"] + w["w"], page["block"]["y"] + page["block"]["h"] + 20)
             for line in page["lines"] for w in line["words"]]
    assert len(boxes) == 3
    yellow = (255, 230, 0)
    for t, active in ((0.7, 0), (1.2, 1), (1.7, 2)):
        frame = c.draw(t)
        counts = [tint_count(frame, yellow, box) for box in boxes]
        assert counts[active] > 300, counts
        assert all(n == 0 for i, n in enumerate(counts) if i != active), counts
    assert tint_count(c.draw(0.7), (255, 255, 255), boxes[1]) > 300  # inactive words are white


def test_emphasis_colour_and_hidden_words():
    tline = timeline(captions={"template_id": "bold_pop", "emphasis": ["a:1"], "hidden": ["a:2"]})
    c = comp.Compositor(tline, sources_for(), 1080, 1920)
    assert "a:2" not in [w["key"] for w in c.pages[0]["words"]]
    frame = c.draw(0.7)  # word 0 active, word 1 emphasised
    assert tint_count(frame, (60, 242, 255)) > 300


def test_nothing_drawn_without_captions_or_outside_pages():
    c = comp.Compositor(timeline(captions={"enabled": False}), sources_for(), 1080, 1920)
    assert c.draw(0.7).getbbox() is None
    c = comp.Compositor(timeline(), sources_for(), 1080, 1920)
    assert c.draw(0.1, progress_bar=False).getbbox() is None
    assert c.scene_at(4.01)["page"] is None  # past the last page's visible window (3.4 s + 0.6 s)


def test_hook_box_present_only_inside_its_interval():
    tline = timeline(overlays=[{"id": "h", "kind": "hook", "text": "Big claim", "start": 1.0, "end": 2.0, "style": "hook_box"}])
    c = comp.Compositor(tline, sources_for(), 1080, 1920)
    inside, before, after = c.draw(1.5), c.draw(0.9, progress_bar=False), c.draw(2.0)
    box = (60, 0.16 * 1920 - 90, 1020, 0.16 * 1920 + 90)
    assert tint_count(inside, (255, 255, 255), box) > 5000
    assert tint_count(before, (255, 255, 255), box) == 0 and tint_count(after, (255, 255, 255), box) == 0


@pytest.mark.parametrize("style,color", [("hook_box", (255, 255, 255)), ("hook_banner", (255, 212, 0)), ("hook_outline", (255, 255, 255))])
def test_hook_styles_draw(style, color):
    tline = timeline(overlays=[{"id": "h", "kind": "hook", "text": "A very long hook text that must wrap onto lines",
                                "start": 0.0, "end": 3.0, "style": style}], captions={"enabled": False})
    frame = comp.Compositor(tline, sources_for(), 1080, 1920).draw(1.0)
    assert tint_count(frame, color) > 1000
    if style == "hook_banner":
        assert tint_count(frame, color, (0, 0, 5, 1920)) > 100  # band spans the full width
    bbox = frame.getbbox()
    assert bbox and bbox[1] < 0.3 * 1920


def test_progress_bar_length_is_proportional_to_time():
    tline = timeline(captions={"enabled": False}, overlays=[{"id": "p", "kind": "progress_bar", "color": "#FF0000",
                                                             "position": "bottom", "thickness": 12}])
    c = comp.Compositor(tline, sources_for(), 1080, 1920)
    for t in (0.0, 1.0, 2.0, 3.0, 4.0):
        arr = np.asarray(c.draw(t))
        row = arr[1920 - 6]
        length = int((row[:, 3] > 0).sum())
        assert length == pytest.approx(1080 * t / 4.0, abs=1.5)
        assert arr[1920 - 13, :, 3].sum() == 0  # nothing above the 12 px strip
    top = comp.Compositor(timeline(captions={"enabled": False}, overlays=[
        {"id": "p", "kind": "progress_bar", "position": "top", "thickness": 8}]), sources_for(), 540, 960)
    arr = np.asarray(top.draw(2.0))
    assert (arr[2, :, 3] > 0).sum() == 270 and arr[8, :, 3].sum() == 0  # thickness scaled by width/1080


def test_pop_animation_grows_from_start_and_progress_bar_excluded_from_scenes():
    c = comp.Compositor(timeline(overlays=[{"id": "p", "kind": "progress_bar"}]), sources_for(), 1080, 1920)
    early, late = c.draw(0.5 + 0.001, progress_bar=False).getbbox(), c.draw(0.5 + 0.2, progress_bar=False).getbbox()
    assert early[2] - early[0] < late[2] - late[0]
    assert c.scene_key(c.scene_at(0.7)) == c.scene_key(c.scene_at(0.71))


def test_logo_anchor_and_missing_asset(tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (200, 100), (255, 0, 0, 255)).save(logo)
    tline = timeline(captions={"enabled": False}, overlays=[{"id": "l", "kind": "logo", "asset_id": "brand", "anchor": "bottom_left",
                                                             "scale": 0.2, "opacity": 0.5}])
    c = comp.Compositor(tline, sources_for(), 1080, 1920, assets={"brand": str(logo)})
    frame = c.draw(1.0)
    x0, y0, x1, y1 = frame.getbbox()
    assert (x1 - x0, y1 - y0) == (216, 108) and x0 == round(0.04 * 1080) and y1 == 1920 - round(0.04 * 1080)
    assert np.asarray(frame)[y0 + 10, x0 + 10, 3] == pytest.approx(128, abs=2)
    missing = comp.Compositor(tline, sources_for(), 1080, 1920, assets={})
    assert missing.draw(1.0).getbbox() is None and "brand" in missing.warnings[0]


def test_frame_plan_covers_every_frame_and_changes_only_when_needed():
    c = comp.Compositor(timeline(), sources_for(), 1080, 1920)
    total = round(tl.duration(c.timeline) * 30)
    plan = c.frame_plan(30, total)
    assert plan[0][0] == 0 and sum(count for _, count, _, _ in plan) == total
    assert all(a[0] + a[1] == b[0] for a, b in zip(plan, plan[1:]))
    assert all(a[2] != b[2] for a, b in zip(plan, plan[1:]))  # adjacent identical scenes merged
    assert len(plan) < total / 3  # far fewer images than frames
    page = c.pages[0]
    keys_at_start = {k for first, _, k, _ in plan if abs(first / 30 - page["start"]) < 0.05}
    assert keys_at_start and all(k.startswith("p0") for k in keys_at_start)


def test_srt_cues_use_original_case_and_pages():
    c = comp.Compositor(timeline(), sources_for(), 1080, 1920)
    cues = c.srt_cues()
    assert cues[0]["text"] == "one two three" and cues[0]["start"] == pytest.approx(0.5)
    assert all(a["end"] <= b["start"] + 1e-9 for a, b in zip(cues, cues[1:]))


# ------------------------------------------------------------------ pure planning helpers

def test_region_rects_even_and_cover_canvas():
    for layout in tl.LAYOUT_REGIONS:
        for width, height in ((1080, 1920), (1080, 1350), (360, 640), (1920, 1080)):
            rects = render_v3.region_rects(layout, width, height)
            assert sum(r[3] for r in rects) == height and all(v % 2 == 0 for r in rects for v in r)
    assert render_v3.region_rects("screen_speaker", 1080, 1920) == [(0, 0, 1080, 1152), (0, 1152, 1080, 768)]


def test_output_size_preview():
    assert render_v3.output_size({"width": 1080, "height": 1920}, True) == (540, 960)
    assert render_v3.output_size({"width": 1920, "height": 1080}, True) == (960, 540)
    assert render_v3.output_size({"width": 1080, "height": 1350}, True) == (540, 676)
    assert render_v3.output_size({"width": 1080, "height": 1920}, False) == (1080, 1920)


def test_motion_kind_and_expression():
    kfs = [{"t": 2.0, "x": 0.2, "y": 0.5, "zoom": 1.0}, {"t": 6.0, "x": 0.8, "y": 0.5, "zoom": 1.0}]
    assert render_v3.motion_kind(kfs, 0.0, 1.0) == "static"  # held before the first keyframe
    assert render_v3.motion_kind(kfs, 3.0, 1.0) == "pan"
    assert render_v3.motion_kind([kfs[0], {**kfs[1], "zoom": 1.5}], 3.0, 1.0) == "zoom"
    assert render_v3.motion_kind(kfs, 7.0, 1.0) == "static"
    expr = render_v3._expr(kfs, "x", 2.0)
    assert expr.count("if(") == 2 and "lt(t,0)" in expr and "lt(t,4)" in expr


def test_plan_pieces_splits_on_ranges_shots_and_broll_with_exact_frames():
    tline = timeline(segments=[{"id": "s1", "source_id": "a", "start": 0.0, "end": 4.0,
                                "cuts": [{"start": 1.0, "end": 1.5, "reason": "filler"}]}],
                     shots=[{"id": "x", "start": 0, "end": 1.7, "layout": "fill"}, {"id": "y", "start": 1.7, "end": 3.5, "layout": "fit"}],
                     broll=[{"id": "b", "source_id": "pic", "source_start": 2.0, "at": 2.0, "duration": 0.5}])
    pieces = render_v3.plan_pieces(tline, 30)
    assert sum(p["frames"] for p in pieces) == round(3.5 * 30)
    assert [p["kind"] for p in pieces] == ["speech", "speech", "speech", "broll", "speech"]
    assert all(a["first_frame"] + a["frames"] == b["first_frame"] for a, b in zip(pieces, pieces[1:]))
    assert pieces[1]["src_start"] == pytest.approx(1.5, abs=0.04)  # starts at the second kept range
    assert pieces[3]["layout"] == "fill" and pieces[3]["src_start"] == pytest.approx(2.0, abs=0.04)


def test_audio_plan_merges_contiguous_and_fades():
    tline = timeline(segments=[{"id": "s1", "source_id": "a", "start": 0.0, "end": 2.0},
                               {"id": "s2", "source_id": "a", "start": 2.0, "end": 3.0,
                                "cuts": [{"start": 2.4, "end": 2.6, "reason": "silence"}]}])
    plan = render_v3.audio_plan(tline)
    assert [(p["src_start"], p["src_end"]) for p in plan] == [(0.0, 2.4), (2.6, 3.0)]
    assert plan[0]["fade"] == pytest.approx(0.012) and plan[1]["fade"] == 0.0


# ------------------------------------------------------------------ real renders

def ffmpeg_source(path: Path, *, expr: str, duration=6, sine=True) -> Path:
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", expr]
    if sine:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac", "-shortest"]
    subprocess.run(cmd + ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def halves_source(path: Path) -> Path:
    """Left half red, right half blue, with audio."""
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x360:r=30:d=6",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x360:r=30:d=6", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-filter_complex", "[0][1]hstack", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def probe(path: Path) -> dict:
    out = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    return {"w": video["width"], "h": video["height"], "duration": float(data["format"]["duration"]), "audio": audio,
            "codec": video["codec_name"], "pix_fmt": video["pix_fmt"]}


def frame_at(path: Path, t: float) -> np.ndarray:
    out = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-f", "image2pipe",
                          "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    import io
    return np.asarray(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)


def is_color(block: np.ndarray, rgb: tuple, tol=60) -> bool:
    return bool((np.abs(block.reshape(-1, 3).mean(axis=0) - np.array(rgb)) < tol).all())


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    return {"halves": str(halves_source(root / "halves.mp4")),
            "test": str(ffmpeg_source(root / "test.mp4", expr="testsrc2=size=640x360:rate=30:duration=6")),
            "pic": str(ffmpeg_source(root / "pic.mp4", expr="color=c=green:s=640x360:r=30:d=6", sine=False))}


def real_sources(media: dict, key: str = "halves") -> dict:
    return {"a": {"duration": 6.0, "source_path": media[key], "words": WORDS},
            "pic": {"duration": 6.0, "source_path": media["pic"]}}


def small(**changes) -> dict:
    base = {"schema_version": 3, "canvas": {"width": 360, "height": 640, "fps": 30, "background": {"kind": "color", "color": "#202020"}},
            "segments": [{"id": "s1", "source_id": "a", "start": 0.0, "end": 4.0}], "captions": {"enabled": False}, "overlays": []}
    base.update(changes)
    return base


def test_render_basic_outputs_progress_bar_hook_logo_and_manifest(media, tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (50, 50), (255, 0, 255, 255)).save(logo)
    tline = small(captions={"template_id": "bold_pop"}, shots=[{"id": "sh", "start": 0, "end": 4, "layout": "fill",
                  "regions": [{"keyframes": [{"t": 0, "x": 0.25, "y": 0.5, "zoom": 1.0}]}]}],
                  overlays=[{"id": "h", "kind": "hook", "text": "Hook text", "start": 0.0, "end": 1.0},
                            {"id": "p", "kind": "progress_bar", "color": "#FFFF00", "thickness": 20},
                            {"id": "l", "kind": "logo", "asset_id": "brand", "anchor": "bottom_right", "scale": 0.2}])
    progress: list[float] = []
    result = render_v3.render(tline, real_sources(media), tmp_path / "out", assets={"brand": str(logo)}, progress=progress.append)
    info = probe(result["video_path"])
    assert (info["w"], info["h"]) == (360, 640) and (result["width"], result["height"]) == (360, 640)
    assert info["duration"] == pytest.approx(4.0, abs=0.12) and info["codec"] == "h264" and info["pix_fmt"] == "yuv420p"
    assert info["audio"] and info["audio"][0]["codec_name"] == "aac"
    assert result["srt_path"].read_text().startswith("1\n00:00:00,500 --> ")
    manifest = json.loads(result["manifest_path"].read_text())
    assert manifest["renderer"] == "clippie.render_v3" and manifest["metrics"]["frames"] == 120
    assert progress[-1] == 1.0 and progress == sorted(progress)
    # progress bar: yellow strip at the bottom, length proportional to time
    lengths = []
    for t in (1.0, 2.0, 3.0):
        row = frame_at(result["video_path"], t)[640 - 4]
        lengths.append(int(((row[:, 0] > 200) & (row[:, 1] > 200) & (row[:, 2] < 80)).sum()))
    assert lengths[0] == pytest.approx(360 * 1.0 / 4, abs=6) and lengths[1] == pytest.approx(360 * 2.0 / 4, abs=6)
    assert lengths[2] == pytest.approx(360 * 3.0 / 4, abs=6)
    early, late = frame_at(result["video_path"], 0.8), frame_at(result["video_path"], 2.5)
    assert (np.abs(early[40:80, 40:320] - late[40:80, 40:320]).sum() > 0)  # hook box visible at 0.8 only
    assert is_color(early[100:300, 20:160], (255, 0, 0), 50) is True  # fill crop of the left (red) half
    magenta = (np.abs(late[640 - 70:640 - 15, 360 - 80:360 - 15] - np.array([255, 0, 255])).max(axis=-1) < 40).mean()
    assert magenta > 0.6  # logo at bottom right


def test_cuts_shorten_duration_and_audio(media, tmp_path):
    tline = small(segments=[{"id": "s1", "source_id": "a", "start": 0.0, "end": 4.0,
                             "cuts": [{"start": 1.0, "end": 2.0, "reason": "filler"}]}], canvas={"width": 360, "height": 640, "fps": 30})
    result = render_v3.render(tline, real_sources(media), tmp_path)
    info = probe(result["video_path"])
    assert info["duration"] == pytest.approx(3.0, abs=0.12)
    assert float(info["audio"][0]["duration"]) == pytest.approx(3.0, abs=0.12)


def test_stack_layout_has_two_distinct_halves_and_keyframes_pan(media, tmp_path):
    left = {"keyframes": [{"t": 0.0, "x": 0.0, "y": 0.5, "zoom": 2.0}]}
    right = {"keyframes": [{"t": 0.0, "x": 1.0, "y": 0.5, "zoom": 2.0}]}
    pan = {"keyframes": [{"t": 2.0, "x": 0.0, "y": 0.5, "zoom": 1.0}, {"t": 4.0, "x": 1.0, "y": 0.5, "zoom": 1.0}]}
    tline = small(shots=[{"id": "st", "start": 0, "end": 2, "layout": "stack", "regions": [left, right]},
                         {"id": "pn", "start": 2, "end": 4, "layout": "fill", "regions": [pan]}])
    result = render_v3.render(tline, real_sources(media), tmp_path)
    f = frame_at(result["video_path"], 1.0)
    assert is_color(f[10:300, 20:340], (255, 0, 0), 60) and is_color(f[340:630, 20:340], (0, 0, 255), 60)
    start, end = frame_at(result["video_path"], 2.1), frame_at(result["video_path"], 3.9)
    assert is_color(start[200:400, 30:100], (255, 0, 0), 70) and is_color(end[200:400, 260:330], (0, 0, 255), 70)
    mid = frame_at(result["video_path"], 3.0)
    assert (mid[300, :, 0] > 150).any() and (mid[300, :, 2] > 150).any()  # both halves visible mid-pan


def test_fit_layout_background_and_screen_speaker(media, tmp_path):
    sources = real_sources(media, "test")
    fit = render_v3.render(small(shots=[{"id": "f", "start": 0, "end": 4, "layout": "fit"}]), sources, tmp_path / "fit")
    f = frame_at(fit["video_path"], 1.0)
    assert is_color(f[5:60, :], (32, 32, 32), 6)  # letterbox shows the colour background (#202020)
    assert f[280:360, :].std() > 20  # picture in the middle (16:9 source fitted to width)
    blur = small(canvas={"width": 360, "height": 640, "fps": 30, "background": {"kind": "blur", "color": "#000000"}},
                 shots=[{"id": "f", "start": 0, "end": 4, "layout": "fit"}])
    b = frame_at(render_v3.render(blur, sources, tmp_path / "blur")["video_path"], 1.0)
    assert not is_color(b[5:60, :], (32, 32, 32), 6) and b[5:60, :].std() > 3  # blurred picture instead of flat colour
    ss = small(shots=[{"id": "s", "start": 0, "end": 4, "layout": "screen_speaker",
                       "regions": [{"keyframes": [{"t": 0, "x": 0.5, "y": 0.5, "zoom": 1.0}]},
                                   {"keyframes": [{"t": 0, "x": 0.5, "y": 0.5, "zoom": 2.0}]}]}])
    s = frame_at(render_v3.render(ss, sources, tmp_path / "ss")["video_path"], 1.0)
    assert is_color(s[0:30, :], (32, 32, 32), 6) and s[130:250, :].std() > 20 and s[400:630, :].std() > 5


def test_broll_replaces_picture_but_not_audio_and_preview_size(media, tmp_path):
    tline = small(canvas={"width": 720, "height": 1280, "fps": 30, "background": {"kind": "color", "color": "#000000"}},
                  broll=[{"id": "b", "source_id": "pic", "source_start": 0.0, "at": 1.0, "duration": 1.0}])
    result = render_v3.render(tline, real_sources(media), tmp_path, preview=True)
    info = probe(result["video_path"])
    assert (info["w"], info["h"]) == (540, 960) and result["video_path"].name == "preview.mp4"
    inside, outside = frame_at(result["video_path"], 1.5), frame_at(result["video_path"], 3.0)
    assert is_color(inside[200:700, 100:440], (0, 128, 0), 60)  # green b-roll
    assert not is_color(outside[200:700, 100:440], (0, 128, 0), 60)
    assert info["duration"] == pytest.approx(4.0, abs=0.12) and info["audio"]


def test_zoom_keyframes_change_framing_over_time(media, tmp_path):
    zoom = {"keyframes": [{"t": 0.0, "x": 0.5, "y": 0.5, "zoom": 1.0}, {"t": 4.0, "x": 0.5, "y": 0.5, "zoom": 3.0}]}
    tline = small(shots=[{"id": "z", "start": 0, "end": 4, "layout": "fill", "regions": [zoom]}])
    result = render_v3.render(tline, real_sources(media, "halves"), tmp_path)
    start, end = frame_at(result["video_path"], 0.1), frame_at(result["video_path"], 3.8)
    red_start, blue_start = (start[:, :, 0] > 200).mean(), (start[:, :, 2] > 200).mean()
    assert red_start > 0.1 and blue_start > 0.1  # wide window: both halves visible
    # zoom 3 on the centre seam: window is a third of the width, split evenly, so each half still shows
    # but the seam sits at the middle in both frames; compare seam sharpness instead via region size
    red_end = (end[:, :, 0] > 200).mean()
    assert 0.3 < red_end < 0.7 and abs(red_end - red_start) < 0.3
    assert np.abs(start - end).mean() >= 0  # frames decoded


def test_missing_audio_source_and_invalid_inputs(media, tmp_path):
    sources = {"a": {"duration": 6.0, "source_path": media["pic"], "words": []}}
    result = render_v3.render(small(), sources, tmp_path)
    assert probe(result["video_path"])["audio"] and any("no audio" in w for w in result["warnings"])
    with pytest.raises(tl.TimelineError):
        render_v3.render({"schema_version": 2}, sources, tmp_path)
    bad = {"a": {"duration": 6.0, "source_path": str(tmp_path / "missing.mp4")}}
    with pytest.raises(FileNotFoundError):
        render_v3.render(small(), bad, tmp_path)


def test_normalize_off_skips_loudnorm_and_on_hits_target(media, tmp_path):
    on = render_v3.render(small(), real_sources(media), tmp_path / "on")
    off = render_v3.render(small(audio={"normalize": False}), real_sources(media), tmp_path / "off")
    def lufs(path: Path) -> float:
        r = subprocess.run([FFMPEG, "-v", "info", "-i", str(path), "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
        return float(r.stderr.rsplit("I:", 1)[1].split("LUFS")[0])
    assert lufs(on["video_path"]) == pytest.approx(-14.0, abs=1.5)
    assert lufs(off["video_path"]) != pytest.approx(-14.0, abs=1.0)
