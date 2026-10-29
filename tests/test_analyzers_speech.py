import shutil
import subprocess

import numpy as np
import pytest

from clippie.analyzers import speech

FPS = speech.ANALYSIS_FPS
W, H = 640, 360
A = (0.15, 0.3, 0.12, 0.30)   # x, y, w, h
B = (0.65, 0.3, 0.12, 0.30)


def tracks(boxes, seconds=6.0, fps=4.0):
    out = []
    for i, box in enumerate(boxes, 1):
        n = int(seconds * fps)
        out.append({"id": f"f{i}", "samples": [{"t": round(k / fps, 3), "x": box[0], "y": box[1], "w": box[2], "h": box[3],
                                                "score": 0.9} for k in range(n)]})
    return {"version": 1, "sample_fps": fps, "width": W, "height": H, "tracks": out}


def frames(seconds, moving=(), whole_face=(), seed=0):
    """Gray frames with a static textured background; `moving` mouths flicker, `whole_face` faces flicker everywhere."""
    rng = np.random.default_rng(seed)
    base = rng.integers(90, 120, (H, W)).astype(np.uint8)
    n = int(seconds * FPS) + 1
    for k in range(n):
        frame = base.copy()
        level = 60 if k % 2 else 0
        for box in moving:
            x, y, w, h = box
            frame[int((y + 2 * h / 3) * H):int((y + h) * H), int((x + .2 * w) * W):int((x + .8 * w) * W)] += level
        for box in whole_face:
            x, y, w, h = box
            frame[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)] += level
        yield k / FPS, frame


def loud(times):
    return np.full(len(times), 0.3, np.float32)


def mean_active(result, tid, lo=0, hi=99):
    values = [s["active"] for s in result["per_track"][tid] if lo <= s["t"] < hi]
    return sum(values) / len(values)


def test_speaking_face_scores_higher_than_listener():
    result = speech.from_frames(frames(6, moving=[A]), tracks([A, B]), envelope_fn=loud)
    assert result["audio"] is True
    assert mean_active(result, "f1") > 0.6
    assert mean_active(result, "f2") < 0.1


def test_output_has_one_row_per_track_sample_in_range():
    ft = tracks([A, B])
    result = speech.from_frames(frames(6, moving=[A]), ft, envelope_fn=loud)
    for track in ft["tracks"]:
        rows = result["per_track"][track["id"]]
        assert [r["t"] for r in rows] == [s["t"] for s in track["samples"]]
        assert all(0.0 <= r["active"] <= 1.0 for r in rows)


def test_audio_silence_suppresses_mouth_motion():
    def half_silent(times):
        return np.where(times < 3.0, 0.3, 0.0).astype(np.float32)
    result = speech.from_frames(frames(6, moving=[A]), tracks([A, B]), envelope_fn=half_silent)
    assert mean_active(result, "f1", 0.5, 2.5) > 0.5
    assert mean_active(result, "f1", 3.5, 5.5) < 0.1


def test_motion_only_when_there_is_no_audio():
    result = speech.from_frames(frames(6, moving=[B]), tracks([A, B]), envelope_fn=None)
    assert result["audio"] is False
    assert mean_active(result, "f2") > mean_active(result, "f1") + 0.4


def test_rigid_whole_face_motion_scores_below_mouth_motion():
    result = speech.from_frames(frames(6, moving=[B], whole_face=[A]), tracks([A, B]), envelope_fn=loud)
    assert mean_active(result, "f1") < 0.8 * mean_active(result, "f2")


def test_box_that_moves_over_a_static_picture_creates_no_motion():
    ft = tracks([A])
    for i, s in enumerate(ft["tracks"][0]["samples"]):
        s["x"] = A[0] + 0.001 * i
    result = speech.from_frames(frames(6), ft, envelope_fn=loud)
    assert mean_active(result, "f1") < 0.05


def test_no_frames_gives_zero_activity_not_a_guess():
    result = speech.from_frames(iter([]), tracks([A]), envelope_fn=loud)
    assert all(r["active"] == 0.0 for r in result["per_track"]["f1"])


def test_mouth_region_is_lower_third_inside_the_face():
    x, y, w, h = speech.mouth_box((0.2, 0.3, 0.1, 0.3))
    assert y == pytest.approx(0.3 + 0.2) and y + h == pytest.approx(0.6)
    assert 0.2 < x and x + w < 0.3


def test_audio_envelope_tracks_loudness():
    rate = 16000
    samples = np.concatenate([np.full(rate, 0.5, np.float32), np.zeros(rate, np.float32)])
    env = speech.audio_envelope(samples, rate, np.array([0.5, 1.5]), 0.0)
    assert env[0] == pytest.approx(0.5, abs=1e-3) and env[1] == pytest.approx(0, abs=1e-3)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_end_to_end_on_a_synthetic_video(tmp_path, monkeypatch):
    video = tmp_path / "talk.mp4"
    box = lambda b: f"x={int((b[0] + .2 * b[2]) * W)}:y={int((b[1] + 2 * b[3] / 3) * H)}:w={int(.6 * b[2] * W)}:h={int(b[3] / 3 * H)}"
    vf = (f"drawbox={box(A)}:color=white:t=fill:enable='lt(mod(t,0.1667),0.0833)',format=yuv420p")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x505050:s={W}x{H}:r=25:d=6",
                    "-f", "lavfi", "-i", "sine=f=220:d=6", "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-shortest", str(video)], check=True, timeout=60)
    ft = tracks([A, B])
    result = speech.activity(video, ft, cache_dir=tmp_path / "cache")
    assert result["audio"] is True
    assert mean_active(result, "f1") > mean_active(result, "f2") + 0.3
    assert list((tmp_path / "cache").glob("speech-1-*.json"))
    monkeypatch.setattr(speech._media, "iter_frames", lambda *a, **k: pytest.fail("cache miss"))
    assert speech.activity(video, ft, cache_dir=tmp_path / "cache") == result
