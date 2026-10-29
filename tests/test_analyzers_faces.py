import hashlib
import sys

import pytest

from clippie.analyzers import AnalyzerUnavailable, faces


def det(t, *boxes):
    return (round(t, 3), [(x, y, w, h, 0.9) for x, y, w, h in boxes])


def seq(n, box_at, fps=4.0, t0=0.0):
    return [det(t0 + i / fps, *box_at(i)) for i in range(n)]


def test_iou_basics():
    assert faces.iou((0, 0, .2, .2), (0, 0, .2, .2)) == pytest.approx(1)
    assert faces.iou((0, 0, .1, .1), (.5, .5, .1, .1)) == 0
    assert faces.iou((0, 0, .2, .2), (.1, 0, .2, .2)) == pytest.approx(1 / 3)


def test_single_face_one_track_normalised_and_ordered():
    dets = seq(12, lambda i: [(0.4 + 0.002 * i, 0.3, 0.1, 0.15)])
    tracks = faces.build_tracks(dets, sample_fps=4)
    assert [t["id"] for t in tracks] == ["f1"]
    samples = tracks[0]["samples"]
    assert len(samples) == 12
    assert [s["t"] for s in samples] == sorted(s["t"] for s in samples)
    for s in samples:
        assert 0 <= s["x"] <= 1 and 0 <= s["y"] <= 1 and s["x"] + s["w"] <= 1 and s["y"] + s["h"] <= 1
        assert set(s) == {"t", "x", "y", "w", "h", "score"}


def test_two_faces_stay_separate_and_ids_follow_first_appearance():
    dets = seq(10, lambda i: [(0.1, 0.3, 0.1, 0.15), (0.7, 0.3, 0.1, 0.15)])
    tracks = faces.build_tracks(dets, sample_fps=4)
    assert len(tracks) == 2
    assert tracks[0]["samples"][0]["x"] < tracks[1]["samples"][0]["x"]
    assert {t["id"] for t in tracks} == {"f1", "f2"}


def test_crossing_faces_keep_identity_by_overlap():
    def boxes(i):
        return [(0.1 + 0.05 * i, 0.3, 0.1, 0.15), (0.6 - 0.05 * i, 0.5, 0.1, 0.15)]
    tracks = faces.build_tracks(seq(10, boxes), sample_fps=4)
    assert len(tracks) == 2
    for t in tracks:
        ys = {round(s["y"], 1) for s in t["samples"]}
        assert len(ys) == 1  # never jumped to the other face's row


def test_gap_tolerance_bridges_missed_detections():
    dets = seq(12, lambda i: [(0.4, 0.3, 0.1, 0.15)])
    for i in (4, 5, 6):  # 0.75 s without a detection
        dets[i] = (dets[i][0], [])
    tracks = faces.build_tracks(dets, sample_fps=4, max_gap=1.0)
    assert len(tracks) == 1 and len(tracks[0]["samples"]) == 9


def test_long_gap_starts_a_new_track():
    dets = seq(16, lambda i: [(0.4, 0.3, 0.1, 0.15)])
    for i in range(5, 11):  # 1.5 s missing
        dets[i] = (dets[i][0], [])
    tracks = faces.build_tracks(dets, sample_fps=4, max_gap=1.0)
    assert len(tracks) == 2


def test_short_blips_are_dropped():
    dets = seq(12, lambda i: [(0.4, 0.3, 0.1, 0.15)] + ([(0.8, 0.8, 0.05, 0.08)] if i in (3, 4) else []))
    assert len(faces.build_tracks(dets, sample_fps=4, min_samples=3)) == 1


def test_fast_mover_keeps_one_track_via_centre_fallback():
    dets = seq(8, lambda i: [(0.1 + 0.08 * i, 0.3, 0.1, 0.15)])  # 0.8 box widths per sample, IoU < 0.25
    assert len(faces.build_tracks(dets, sample_fps=4)) == 1


def test_smoothing_reduces_jitter_and_keeps_the_trend():
    jitter = [0.01, -0.01, 0.012, -0.012] * 5
    dets = seq(20, lambda i: [(0.4 + jitter[i], 0.3 - jitter[i], 0.1, 0.15)])
    samples = faces.build_tracks(dets, sample_fps=4)[0]["samples"]
    raw_span = max(jitter) - min(jitter)
    inner = [s["x"] for s in samples[3:-3]]
    assert max(inner) - min(inner) < raw_span / 2
    drift = seq(20, lambda i: [(0.2 + 0.02 * i, 0.3, 0.1, 0.15)])
    xs = [s["x"] for s in faces.build_tracks(drift, sample_fps=4)[0]["samples"]]
    assert xs == sorted(xs) and xs[-1] - xs[0] > 0.3


def test_smoothing_does_not_average_across_a_gap():
    dets = seq(12, lambda i: [(0.2 if i < 6 else 0.7, 0.3, 0.1, 0.15)])
    for i in (5, 6):
        dets[i] = (dets[i][0], [])
    # same track, but the pre-gap samples are never pulled toward post-gap ones beyond radius
    xs = [s["x"] for s in faces.build_tracks(dets, sample_fps=4, max_gap=1.0)[0]["samples"]]
    assert xs[0] == pytest.approx(0.2, abs=1e-3)


def test_boxes_are_clamped_into_the_frame():
    dets = seq(6, lambda i: [(0.95, 0.9, 0.1, 0.2)])
    for s in faces.build_tracks(dets, sample_fps=4)[0]["samples"]:
        assert s["x"] + s["w"] <= 1.0 + 1e-9 and s["y"] + s["h"] <= 1.0 + 1e-9


def test_deterministic():
    dets = seq(10, lambda i: [(0.1, 0.3, 0.1, 0.15), (0.7, 0.3, 0.1, 0.15)])
    assert faces.build_tracks(dets, sample_fps=4) == faces.build_tracks(dets, sample_fps=4)


def test_no_detections():
    assert faces.build_tracks([], sample_fps=4) == []
    assert faces.build_tracks(seq(5, lambda i: []), sample_fps=4) == []


# ---- availability and model handling ---------------------------------------------------

def test_missing_model_is_unavailable_not_fabricated(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    monkeypatch.setenv("CLIPPIE_YUNET_MODEL", str(tmp_path / "nope.onnx"))
    with pytest.raises(AnalyzerUnavailable, match="prepare_vision"):
        faces.track(tmp_path / "video.mp4")


def test_missing_opencv_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "cv2", None)
    with pytest.raises(AnalyzerUnavailable, match="vision"):
        faces.track(tmp_path / "video.mp4")


def test_wrong_checksum_model_is_refused(tmp_path, monkeypatch):
    pytest.importorskip("cv2")
    bad = tmp_path / "m.onnx"
    bad.write_bytes(b"not a model")
    monkeypatch.setenv("CLIPPIE_YUNET_MODEL", str(bad))
    with pytest.raises(AnalyzerUnavailable, match="SHA256"):
        faces.track(tmp_path / "video.mp4")


def test_pinned_hash_looks_like_sha256():
    assert len(faces.MODEL_SHA256) == 64 and int(faces.MODEL_SHA256, 16) >= 0


# ---- scripts/prepare_vision.py ---------------------------------------------------------

@pytest.fixture
def prepare():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("prepare_vision", Path(__file__).resolve().parents[1] / "scripts/prepare_vision.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_downloads_verifies_and_writes_atomically(prepare, tmp_path, monkeypatch):
    payload = b"fake model bytes"
    monkeypatch.setattr(faces, "MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    path = prepare.prepare(tmp_path / "models", fetcher=lambda url: payload, urls=["u"])
    assert path.read_bytes() == payload and path.name == faces.MODEL_NAME
    assert [p.name for p in path.parent.iterdir()] == [faces.MODEL_NAME]


def test_prepare_rejects_checksum_mismatch_and_tries_next_mirror(prepare, tmp_path, monkeypatch):
    good = b"good"
    monkeypatch.setattr(faces, "MODEL_SHA256", hashlib.sha256(good).hexdigest())
    calls = []

    def fetcher(url):
        calls.append(url)
        return b"tampered" if url == "a" else good
    path = prepare.prepare(tmp_path, fetcher=fetcher, urls=["a", "b"])
    assert calls == ["a", "b"] and path.read_bytes() == good


def test_prepare_fails_when_nothing_verifies(prepare, tmp_path):
    with pytest.raises(RuntimeError, match="verified"):
        prepare.prepare(tmp_path, fetcher=lambda url: b"junk", urls=["a"])
    assert list(tmp_path.iterdir()) == []


def test_prepare_skips_download_when_present(prepare, tmp_path, monkeypatch):
    payload = b"model"
    monkeypatch.setattr(faces, "MODEL_SHA256", hashlib.sha256(payload).hexdigest())
    (tmp_path / faces.MODEL_NAME).write_bytes(payload)
    assert prepare.prepare(tmp_path, fetcher=lambda url: pytest.fail("downloaded")) == tmp_path / faces.MODEL_NAME
