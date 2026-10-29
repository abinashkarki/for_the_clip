import json
import shutil
import subprocess

import pytest

from clippie import media
from clippie.analyzers import cache, scenes

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def make_video(path, colours, seconds=2, size="160x90", fps=25):
    inputs, chain = [], ""
    for i, colour in enumerate(colours):
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s={size}:r={fps}:d={seconds}"]
        chain += f"[{i}]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
           f"{chain}concat=n={len(colours)}:v=1:a=0,format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path)]
    subprocess.run(cmd, check=True, timeout=60)
    return path


@pytest.fixture(scope="module")
def cuts_video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("scenes") / "cuts.mp4", ["red", "white", "black", "gray"])


def test_hard_colour_cuts_are_found_at_the_right_times(cuts_video):
    assert scenes.detect(cuts_video) == pytest.approx([2.0, 4.0, 6.0], abs=0.05)


def test_range_offsets_are_source_times(cuts_video):
    assert scenes.detect(cuts_video, start=3.0, end=7.0) == pytest.approx([4.0, 6.0], abs=0.05)


def test_no_cut_in_a_flat_clip(tmp_path):
    assert scenes.detect(make_video(tmp_path / "flat.mp4", ["gray"], seconds=3)) == []


def test_threshold_controls_sensitivity(tmp_path):
    video = make_video(tmp_path / "soft.mp4", ["0x707070", "0x808080"])
    assert scenes.detect(video, threshold=0.3) == []
    assert scenes.detect(video, threshold=0.01) == pytest.approx([2.0], abs=0.05)


def test_empty_range_is_rejected(cuts_video):
    with pytest.raises(ValueError):
        scenes.detect(cuts_video, start=5, end=5)


def test_results_are_cached_by_params(cuts_video, tmp_path, monkeypatch):
    first = scenes.detect(cuts_video, cache_dir=tmp_path)
    assert list(tmp_path.glob("scenes-1-*.json"))
    real = media._run
    monkeypatch.setattr(media, "_run", lambda args, **k: pytest.fail("cache miss") if "ffmpeg" in args[0] else real(args, **k))
    assert scenes.detect(cuts_video, cache_dir=tmp_path) == first


def test_log_parser_merges_flashes_and_ignores_first_frame():
    log = "pts_time:0.04\n pts_time:2.00 \n pts_time:2.10\npts_time:4.50\npts_time:9.99"
    assert scenes.cuts_from_log(log, 10.0, 20.0) == [12.0, 14.5]


def test_cache_key_changes_with_file_and_params(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"1")
    k1 = cache.key(a, "x", 1, {"p": 1})
    assert k1 == cache.key(a, "x", 1, {"p": 1})
    assert k1 != cache.key(a, "x", 2, {"p": 1}) and k1 != cache.key(a, "x", 1, {"p": 2})
    a.write_bytes(b"22")
    assert k1 != cache.key(a, "x", 1, {"p": 1})


def test_cache_ignores_corrupt_and_foreign_entries(tmp_path):
    src = tmp_path / "s.bin"
    src.write_bytes(b"1")
    cache.store(tmp_path / "c", src, "x", 1, {}, {"v": 1})
    (path,) = (tmp_path / "c").glob("x-1-*.json")
    assert cache.load(tmp_path / "c", src, "x", 1, {}) == {"v": 1}
    path.write_text("{not json")
    assert cache.load(tmp_path / "c", src, "x", 1, {}) is None
    path.write_text(json.dumps({"key": "other", "result": 5}))
    assert cache.load(tmp_path / "c", src, "x", 1, {}) is None
