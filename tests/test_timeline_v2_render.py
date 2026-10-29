"""Synthetic media proof for a single, role-free portrait v2 segment."""
import json
import shutil
import subprocess

import numpy as np
import pytest

from clippie import render


def test_v2_single_segment_renders_portrait_with_original_audio_and_captions(tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg tools are required")
    monkeypatch.setenv("CLIPPIE_FFMPEG", ffmpeg)
    monkeypatch.setenv("CLIPPIE_FFPROBE", ffprobe)
    source_path = tmp_path / "synthetic.mp4"
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source_path),
    ], check=True, capture_output=True)
    words = [{"id": i, "text": word, "start": start, "end": end}
             for i, (word, start, end) in enumerate([
                 ("One", .1, .4), ("complete", .4, .7), ("moment.", .7, 1.)])]
    sources = {"synthetic": {"source_id": "synthetic", "source_path": source_path,
                             "duration": 2., "title": "Synthetic fixture", "words": words}}
    timeline = {"schema_version": 2, "width": 360, "height": 640,
                "clips": [{"id": "moment", "source_id": "synthetic", "start": 0, "end": 2,
                           "framing": {"mode": "fill"}}]}
    result = render.render_production(timeline, sources, tmp_path / "rendered")
    info = render._probe(result["video_path"])
    assert (info["width"], info["height"]) == (360, 640)
    assert info["duration"] == pytest.approx(2., abs=.12)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    # Verify the original synthetic audio survives, rather than just an audio stream existing.
    audio = subprocess.run([ffmpeg, "-v", "error", "-i", str(result["video_path"]),
                            "-t", "1", "-vn", "-ac", "1", "-ar", "8000",
                            "-f", "f32le", "pipe:1"], check=True, capture_output=True)
    samples = np.frombuffer(audio.stdout, dtype=np.float32)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    assert np.fft.rfftfreq(len(samples), 1 / 8000)[spectrum.argmax()] == pytest.approx(440, abs=10)
    assert "00:00:00,100 --> 00:00:01,000\nOne complete moment." in result["srt_path"].read_text()
    manifest = json.loads(result["manifest_path"].read_text())
    assert manifest["timeline"]["schema_version"] == 2
    assert manifest["timeline"]["aspect_ratio"] == "9:16"
    assert len(manifest["timeline"]["clips"]) == 1
    assert "role" not in manifest["timeline"]["clips"][0]
    assert manifest["metrics"]["caption_words"] == 3
    assert any(row["active_word"] for row in manifest["overlay_schedule"])
    # The burned-in caption plate must change the otherwise uniform red picture.
    frame = subprocess.run([ffmpeg, "-v", "error", "-ss", "0.2", "-i", str(result["video_path"]),
                            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                           check=True, capture_output=True)
    pixels = np.frombuffer(frame.stdout, dtype=np.uint8).reshape(640, 360, 3)
    assert pixels[540:620, 60:300, 0].min() < 100
