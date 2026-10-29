"""Face tracks: YuNet detections on sampled frames, IoU tracking, temporal smoothing.

Boxes are normalised 0-1 (x, y = top-left) on the source frame, times are source seconds.
Detection runs on frames scaled to `detect_width`, so faces smaller than about 3% of the
frame width are not found.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import AnalyzerUnavailable, _media, cache
from ..paths import model_directory

VERSION = 1
MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
MODEL_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + MODEL_NAME)

_VERIFIED: dict[str, str] = {}


def model_path() -> Path:
    return model_directory(MODEL_NAME, override='CLIPPIE_YUNET_MODEL')


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise AnalyzerUnavailable("Face tracking needs OpenCV: uv sync --extra vision") from exc
    return cv2


def _verified_model() -> str:
    path = model_path()
    if not path.is_file():
        raise AnalyzerUnavailable(f"YuNet model not found at {path}. Run: python scripts/prepare_vision.py")
    key = str(path.resolve())
    if key not in _VERIFIED:
        digest = sha256_file(path)
        if digest != MODEL_SHA256:
            raise AnalyzerUnavailable(f"YuNet model at {path} fails its SHA256 check; rerun scripts/prepare_vision.py --force")
        _VERIFIED[key] = digest
    return key


# ---- tracking (pure Python, no model needed) -------------------------------------------

def iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = min(ax2, bx2) - max(a[0], b[0]), min(ay2, by2) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def _affinity(last, box) -> float:
    """IoU, with a centre-distance fallback so a fast mover at low sample rate stays one track."""
    overlap = iou(last, box)
    if overlap >= 0.25:
        return overlap
    ratio = box[2] / last[2] if last[2] else 0
    dx = abs((box[0] + box[2] / 2) - (last[0] + last[2] / 2))
    dy = abs((box[1] + box[3] / 2) - (last[1] + last[3] / 2))
    if 0.6 <= ratio <= 1.67 and dx < 0.9 * max(box[2], last[2]) and dy < 0.6 * max(box[3], last[3]):
        return 0.2 - 0.1 * (dx / max(box[2], last[2]) + dy / max(box[3], last[3]))
    return 0.0


def link(detections, *, max_gap=1.0):
    """detections: [(t, [(x, y, w, h, score), ...]), ...] sorted by t -> list of raw tracks.

    Greedy highest-affinity matching; a track survives `max_gap` seconds without a match.
    """
    tracks, active = [], []
    for t, boxes in detections:
        active = [tr for tr in active if t - tr[-1]["t"] <= max_gap + 1e-9]
        pairs = sorted(((_affinity(tr[-1]["box"], box[:4]), ti, bi)
                        for ti, tr in enumerate(active) for bi, box in enumerate(boxes)),
                       key=lambda p: (-p[0], p[1], p[2]))
        used_t, used_b = set(), set()
        for score, ti, bi in pairs:
            if score <= 0 or ti in used_t or bi in used_b:
                continue
            used_t.add(ti)
            used_b.add(bi)
            active[ti].append({"t": t, "box": tuple(boxes[bi][:4]), "score": boxes[bi][4]})
        for bi, box in enumerate(boxes):
            if bi not in used_b:
                tr = [{"t": t, "box": tuple(box[:4]), "score": box[4]}]
                tracks.append(tr)
                active.append(tr)
    return tracks


def _median3(values):
    out = list(values)
    for i in range(1, len(values) - 1):
        out[i] = sorted(values[i - 1:i + 2])[1]
    return out


def smooth(track, *, radius=0.6):
    """Median-of-3 then triangular moving average over +-radius s, never across the track's own gaps."""
    times = [s["t"] for s in track]
    channels = [_median3([s["box"][k] for s in track]) for k in range(4)]
    result = []
    for i, t in enumerate(times):
        weights, acc = 0.0, [0.0] * 4
        for j, u in enumerate(times):
            d = abs(u - t)
            if d <= radius:
                w = 1.0 - d / (radius + 1e-9) * 0.9
                weights += w
                for k in range(4):
                    acc[k] += w * channels[k][j]
        result.append(tuple(a / weights for a in acc))
    return result


def build_tracks(detections, *, sample_fps, max_gap=1.0, min_samples=3):
    """Raw detections -> contract `tracks` (ids f1.., samples with normalised boxes)."""
    raw = [tr for tr in link(detections, max_gap=max_gap) if len(tr) >= min_samples]
    raw.sort(key=lambda tr: (tr[0]["t"], tr[0]["box"][0]))
    tracks = []
    for n, tr in enumerate(raw, 1):
        boxes = smooth(tr)
        samples = []
        for s, (x, y, w, h) in zip(tr, boxes):
            x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
            w, h = min(w, 1.0 - x), min(h, 1.0 - y)
            samples.append({"t": round(s["t"], 3), "x": round(x, 4), "y": round(y, 4),
                            "w": round(w, 4), "h": round(h, 4), "score": round(float(s["score"]), 3)})
        tracks.append({"id": f"f{n}", "samples": samples})
    return tracks


# ---- detection --------------------------------------------------------------------------

def _detect_frames(source_path, start, end, sample_fps, detect_width, score_threshold, info):
    cv2 = _load_cv2()
    model = _verified_model()
    w, h = _media.scaled_size(info, detect_width)
    detector = cv2.FaceDetectorYN.create(model, "", (w, h), score_threshold, 0.3, 50)
    detections = []
    for t, frame in _media.iter_frames(source_path, start=start, end=end, fps=sample_fps,
                                       width=detect_width, pix_fmt="bgr24", size=(w, h)):
        _, found = detector.detect(frame)
        boxes = []
        if found is not None:
            for row in found:
                x, y, bw, bh, score = (float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[14]))
                x0, y0 = max(0.0, x), max(0.0, y)
                x1, y1 = min(float(w), x + bw), min(float(h), y + bh)
                if x1 - x0 > 2 and y1 - y0 > 2:
                    boxes.append((x0 / w, y0 / h, (x1 - x0) / w, (y1 - y0) / h, score))
        detections.append((t, boxes))
    return detections


def track(source_path, *, start=0, end=None, sample_fps=4.0, cache_dir=None,
          detect_width=640, score_threshold=0.6) -> dict:
    """Face tracks over [start, end). Raises AnalyzerUnavailable without OpenCV or the model."""
    _load_cv2()
    _verified_model()
    info = _media.probe(source_path)
    start, end = _media.span(info, start, end)
    params = {"start": round(start, 3), "end": round(end, 3), "sample_fps": float(sample_fps),
              "detect_width": int(detect_width), "score_threshold": float(score_threshold),
              "model": MODEL_SHA256}
    cached = cache.load(cache_dir, source_path, "faces", VERSION, params)
    if cached is not None:
        return cached
    detections = _detect_frames(source_path, start, end, sample_fps, detect_width, score_threshold, info)
    result = {"version": VERSION, "sample_fps": float(sample_fps), "width": info["width"],
              "height": info["height"],
              "tracks": build_tracks(detections, sample_fps=sample_fps)}
    cache.store(cache_dir, source_path, "faces", VERSION, params, result)
    return result
