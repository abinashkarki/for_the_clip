import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from clippie import render


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="FFmpeg tools are required")


@pytest.fixture
def two_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("CLIPPIE_FFMPEG", FFMPEG or "ffmpeg")
    monkeypatch.setenv("CLIPPIE_FFPROBE", FFPROBE or "ffprobe")
    sources = {}
    for sid, color, frequency in (("red", "red", 440), ("blue", "blue", 880)):
        path = tmp_path / f"{sid}.mp4"
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i",
             f"color=c={color}:s=320x180:r=30:d=2", "-f", "lavfi", "-i",
             f"sine=frequency={frequency}:sample_rate=48000:duration=2", "-map", "0:v:0", "-map", "1:a:0",
             "-vf", "drawbox=x=0:y=0:w=20:h=180:color=lime:t=fill",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
            check=True, capture_output=True,
        )
        sources[sid] = {"source_id": sid, "source_path": path, "duration": 2.0,
                        "title": sid.title(), "words": []}
    sources["red"]["words"] = [
        {"id": 0, "text": "The", "start": 0.10, "end": 0.25},
        {"id": 1, "text": "first", "start": 0.25, "end": 0.55},
        {"id": 2, "text": "idea.", "start": 0.55, "end": 0.90},
        {"id": 3, "text": "Join", "start": 1.30, "end": 1.45},
    ]
    sources["blue"]["words"] = [
        {"id": 0, "text": "Then", "start": 0.20, "end": 0.35},
        {"id": 1, "text": "next.", "start": 0.35, "end": 0.65},
    ]
    return sources


def _decode_video_frame(path: Path, at: float, width: int, height: int) -> np.ndarray:
    result = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1", "-f", "rawvideo",
         "-pix_fmt", "rgb24", "pipe:1"], check=True, capture_output=True,
    )
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(height, width, 3)


def _dominant_frequency(path: Path, at: float) -> float:
    result = subprocess.run(
        [FFMPEG, "-v", "error", "-ss", str(at), "-i", str(path), "-t", "0.35", "-vn", "-ac", "1",
         "-ar", "8000", "-f", "f32le", "pipe:1"], check=True, capture_output=True,
    )
    samples = np.frombuffer(result.stdout, dtype=np.float32)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1 / 8000)
    return float(freqs[int(np.argmax(spectrum))])


@pytest.mark.parametrize("canvas", ["plain", "blurred"])
def test_renders_two_source_speech_edit_with_picture_only_broll_and_burned_captions(
    two_sources: dict, tmp_path: Path, canvas,
) -> None:
    timeline = {
        "canvas_style": canvas,
        "clips": [
            {"id": "primary-a", "source_id": "red", "start": 0, "end": 1.5,
             "role": "hook", "note": "Opening", "framing": {"mode": "fill", "zoom": 1.15, "x": 0.5, "y": 0.5}},
            {"id": "primary-b", "source_id": "blue", "start": 0, "end": 1.0,
             "role": "payoff", "note": "Response", "framing": {"mode": "fit", "zoom": 1, "x": 0.5, "y": 0.5}},
        ],
        "broll": [{"id": "overlay", "source_id": "blue", "start": 0.2, "end": 0.6, "at": 0.5,
                   "note": "Visual overlay", "framing": {"mode": "fit", "zoom": 1, "x": 0.5, "y": 0.5}}],
        "titles": [{"text": "A clearer idea", "at": 0, "duration": 0.4}],
    }
    progress = []
    result = render.render_production(timeline, two_sources, tmp_path / "render", progress.append)
    assert result["video_path"].is_file()
    assert result["srt_path"].is_file()
    assert result["manifest_path"].is_file()
    assert progress == sorted(progress) and progress[-1] == 1.0

    info = render._probe(result["video_path"])
    assert (info["width"], info["height"]) == (1280, 720)  # contract default is landscape
    assert info["duration"] == pytest.approx(2.5, abs=0.12)
    assert info["video_codec"] == "h264" and info["pixel_format"] == "yuv420p"
    assert info["audio_codec"] == "aac"

    # The picture turns blue during the overlay while the narration remains the
    # original red source's 440 Hz speech bed. The next primary edit carries 880 Hz.
    frame = _decode_video_frame(result["video_path"], 0.72, 1280, 720)
    rgb = frame[300:420, 500:780].mean(axis=(0, 1))
    assert rgb[2] > rgb[0] * 1.4
    crop_frame = _decode_video_frame(result["video_path"], 0.05, 1280, 720)
    edge_pixel = crop_frame[360, 3]
    assert edge_pixel[0] > 180 and edge_pixel[1] < 100  # zoomed fill cropped the source's lime edge bar
    caption_frame = _decode_video_frame(result["video_path"], 0.2, 1280, 720)
    assert caption_frame[605:680, 420:860, 0].mean() < 175  # dark phrase plate is burned into the frame
    assert _dominant_frequency(result["video_path"], 0.72) == pytest.approx(440, abs=30)
    assert _dominant_frequency(result["video_path"], 1.85) == pytest.approx(880, abs=35)

    srt = result["srt_path"].read_text()
    assert "00:00:00,100 --> 00:00:00,900\nThe first idea." in srt
    assert "00:00:01,300 --> 00:00:01,450\nJoin" in srt
    assert "00:00:01,700 --> 00:00:02,150\nThen next." in srt
    manifest = json.loads(result["manifest_path"].read_text())
    assert manifest["metrics"]["broll_overlays"] == 1
    assert any(row["active_word"] for row in manifest["overlay_schedule"])
    assert any("correspondence" in warning for warning in result["warnings"])


def test_caption_styles_are_distinct_and_wrap_long_copy_inside_safe_margins(tmp_path: Path) -> None:
    phrase = {"phrase": [{"text": text, "start": i, "end": i + 0.4} for i, text in enumerate(
        ["interconnected", "thermodynamics", "infrastructure", "reconstruction"])] , "active": 2}
    clean, editorial = tmp_path / "clean.png", tmp_path / "editorial.png"
    title = "A long explanatory title that must wrap inside portrait safe margins"
    render._render_overlay_frame(clean, 720, 1280, title, phrase, "clean_focus")
    render._render_overlay_frame(editorial, 720, 1280, title, phrase, "editorial")
    assert Image.open(clean).size == (720, 1280)
    assert clean.read_bytes() != editorial.read_bytes()
    for path in (clean, editorial):
        image = Image.open(path)
        alpha = image.getchannel("A")
        left, _top, right, _bottom = alpha.getbbox()
        assert left >= 720 * 0.02 and right <= 720 * 0.98


@pytest.mark.parametrize('style', ['clean_focus', 'editorial', 'phrase'])
@pytest.mark.parametrize('content', ['title', 'caption'])
def test_portrait_text_keeps_composition_between_preview_and_export(tmp_path, style, content):
    phrase = {'phrase': [{'text': word} for word in ['Keep', 'the', 'complete', 'idea']], 'active': 1}
    bounds = []
    for width, height in [(360, 640), (540, 960), (1080, 1920)]:
        path = tmp_path / f'{content}-{style}-{width}.png'
        render._render_overlay_frame(path, width, height,
            'Keep the complete idea' if content == 'title' else None,
            phrase if content == 'caption' else None, style)
        with Image.open(path) as image:
            left, top, right, bottom = image.getchannel('A').getbbox()
        assert left >= width * .02 and right <= width * .98
        bounds.append((left / width, top / height, right / width, bottom / height))
    for observed in bounds[:-1]:
        assert observed == pytest.approx(bounds[-1], abs=.015)


def test_same_source_out_of_order_excerpts_use_bounded_inputs_and_rebase_words(
    two_sources: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = two_sources["red"]
    source["words"] = [
        {"id": 0, "text": "late", "start": 1.20, "end": 1.35},
        {"id": 1, "text": "early", "start": 0.05, "end": 0.20},
    ]
    timeline = {"clips": [
        {"id": "late", "source_id": "red", "start": 1.1, "end": 1.5, "role": "hook",
         "framing": {"mode": "fit", "zoom": 1, "x": 0.5, "y": 0.5}},
        {"id": "early", "source_id": "red", "start": 0, "end": 0.4, "role": "payoff",
         "framing": {"mode": "fit", "zoom": 1, "x": 0.5, "y": 0.5}},
    ]}
    commands = []
    actual_run = render._run

    def recording_run(args, *, timeout, what):
        if what == "Production render":
            commands.append(list(args))
        return actual_run(args, timeout=timeout, what=what)

    monkeypatch.setattr(render, "_run", recording_run)
    result = render.render_production(timeline, two_sources, tmp_path / "out-of-order")

    assert result["video_path"].is_file()
    info = render._probe(result["video_path"])
    assert info["duration"] == pytest.approx(0.8, abs=0.12)
    srt = result["srt_path"].read_text()
    assert "00:00:00,100 --> 00:00:00,250\nlate" in srt
    assert "00:00:00,450 --> 00:00:00,600\nearly" in srt

    command = commands[0]
    input_positions = [i for i, value in enumerate(command) if value == "-i"]
    assert len(input_positions) == 3  # one bounded input per excerpt plus caption overlay
    expected_windows = [(1.1, 0.4), (0.0, 0.4)]
    for position, (start, span) in zip(input_positions[:2], expected_windows):
        assert command[position - 4:position] == ["-ss", f"{start:.6f}", "-t", f"{span:.6f}"]
    graph = command[command.index("-filter_complex") + 1]
    assert "[0:v:0]trim=start=0:end=0.400000" in graph
    assert "[1:v:0]trim=start=0:end=0.400000" in graph
    assert "atrim=start=1.100000" not in graph and "atrim=start=0:end=0.400000" in graph


@pytest.mark.parametrize('style,steady', [('phrase', True), ('clean_focus', False), ('editorial', False)])
def test_phrase_profile_pixels_ignore_active_word_but_existing_profiles_follow_it(tmp_path, style, steady):
    phrase = [{'text': word} for word in ['Keep', 'the', 'complete', 'idea']]
    images = []
    for active in [0, 2]:
        path = tmp_path / f'{style}-{active}.png'
        render._render_overlay_frame(path, 360, 640, None, {'phrase': phrase, 'active': active}, style)
        with Image.open(path) as image:
            images.append(image.tobytes())
            if steady:
                assert (255, 255, 255, 255) in image.getdata()
                assert (216, 246, 111, 255) not in image.getdata()
    assert (images[0] == images[1]) is steady


def test_phrase_overlay_reuses_frame_and_preserves_caption_time_pause_and_source_cut_boundaries(tmp_path):
    sources = {'s': {'words': [{'text': 'Keep', 'start': 0, 'end': .2},
                              {'text': 'context', 'start': .25, 'end': .5},
                              {'text': 'after', 'start': 1.4, 'end': 1.6},
                              {'text': 'cut', 'start': 2, 'end': 2.2}]}}
    timeline = {'captions': True, 'clips': [{'source_id': 's', 'start': 0, 'end': 1.8, 'at': 0},
                                         {'source_id': 's', 'start': 2, 'end': 2.4, 'at': 1.8}]}
    reference_cues, reference_events = render._caption_data(timeline, sources)
    assert [c['text'] for c in reference_cues] == ['Keep context', 'after', 'cut']
    schedules = {}
    for style in ['phrase', 'clean_focus']:
        styled = dict(timeline, caption_style=style)
        cues, events = render._caption_data(styled, sources)
        assert cues == reference_cues and events == reference_events
        _, schedule = render._overlay_input(tmp_path / style, styled, cues, events, 2.2, 360, 640)
        schedules[style] = schedule
        assert any(row['caption'] is None and row['start'] >= .5 and row['end'] <= 1.4 for row in schedule)
        if style == 'phrase':
            assert all(row['active_word'] is None for row in schedule)
        else:
            assert any(row['active_word'] for row in schedule)
    assert [{k: v for k, v in row.items() if k != 'active_word'} for row in schedules['phrase']] == [
           {k: v for k, v in row.items() if k != 'active_word'} for row in schedules['clean_focus']]
    assert len(list((tmp_path / 'phrase' / 'caption-frames').glob('*.png'))) == 4  # Three phrases plus blank.
    assert len(list((tmp_path / 'clean_focus' / 'caption-frames').glob('*.png'))) == 5


def test_blurred_portrait_supporting_fit_uses_its_own_picture_but_primary_audio(two_sources, tmp_path):
    timeline = {'schema_version': 2, 'width': 360, 'height': 640, 'canvas_style': 'blurred',
                'captions': False, 'audio': {'normalize': False},
                'clips': [{'id': 'primary', 'source_id': 'red', 'start': 0, 'end': 1,
                           'framing': {'mode': 'fit'}}],
                'broll': [{'id': 'support', 'source_id': 'blue', 'start': 0, 'end': .6, 'at': .2,
                           'framing': {'mode': 'fit'}}]}
    result = render.render_production(timeline, two_sources, tmp_path / 'support-canvas')
    frame = _decode_video_frame(result['video_path'], .4, 360, 640)
    for area in [frame[30:100, 100:260], frame[270:370, 100:260]]:
        rgb = area.mean(axis=(0, 1))
        assert rgb[2] > rgb[0] * 2 and rgb[2] > 80
    assert _dominant_frequency(result['video_path'], .4) == pytest.approx(440, abs=30)


def test_canvas_option_does_not_change_manual_fill_frames(two_sources, tmp_path):
    base = {'schema_version': 2, 'width': 360, 'height': 640, 'captions': False,
            'clips': [{'id': 'primary', 'source_id': 'red', 'start': 0, 'end': .5,
                       'framing': {'mode': 'fill', 'zoom': 1.2, 'x': .1, 'y': .8}}]}
    frames = []
    for style in ['plain', 'blurred']:
        output = render.render_production(dict(base, canvas_style=style), two_sources, tmp_path / style)
        frames.append(_decode_video_frame(output['video_path'], .2, 360, 640))
    assert np.array_equal(*frames)
