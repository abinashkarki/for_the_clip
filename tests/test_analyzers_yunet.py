"""Integration with the real YuNet model; skipped when OpenCV, the model or FFmpeg is absent.

Set CLIPPIE_TEST_FACE_IMAGE to a photo containing one frontal face to also run the positive test.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("cv2")
from clippie.analyzers import faces  # noqa: E402

pytestmark = [pytest.mark.skipif(not faces.model_path().is_file(), reason="YuNet model not prepared (scripts/prepare_vision.py)"),
              pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")]


def video_from_image(image, path, seconds=3, size="640x360"):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-t", str(seconds), "-i", str(image),
                    "-vf", f"scale={size},setsar=1,fps=25,format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path)],
                   check=True, timeout=60)
    return path


def test_model_checksum_matches_pin():
    assert faces.sha256_file(faces.model_path()) == faces.MODEL_SHA256


def test_blank_video_has_no_faces(tmp_path):
    video = tmp_path / "blank.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x406080:s=640x360:r=25:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, timeout=60)
    result = faces.track(video, cache_dir=tmp_path / "cache")
    assert result["tracks"] == [] and result["width"] == 640 and result["height"] == 360


@pytest.mark.skipif(not os.environ.get("CLIPPIE_TEST_FACE_IMAGE"), reason="CLIPPIE_TEST_FACE_IMAGE not set")
def test_real_face_is_tracked_and_cached(tmp_path):
    video = video_from_image(Path(os.environ["CLIPPIE_TEST_FACE_IMAGE"]), tmp_path / "face.mp4")
    result = faces.track(video, cache_dir=tmp_path / "cache")
    assert len(result["tracks"]) == 1
    samples = result["tracks"][0]["samples"]
    assert len(samples) >= 10 and all(0 < s["w"] < 1 and 0 < s["h"] < 1 for s in samples)
    assert list((tmp_path / "cache").glob("faces-1-*.json"))
    assert faces.track(video, cache_dir=tmp_path / "cache") == result
