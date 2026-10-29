"""Who is speaking: mouth-region motion energy correlated with the audio RMS envelope.

For each face-track sample the result is 0..1 "active". Method: decode gray frames at
ANALYSIS_FPS, take the lower third of each face box (the mouth), measure frame-to-frame
absolute difference inside that fixed region (so a moving box adds no motion), subtract half
the difference in the upper half of the box (rigid head motion), scale by one pooled scale
across all tracks, then gate by audio loudness (geometric mean) and weight each track by how
well its motion correlates with the audio envelope (weight 0.6 to 1.0; no penalty when either
signal is constant).

Limits. Not lip-sync: any mouth-area motion while anyone is loud counts, so a listener who
laughs or nods during speech scores some activity. A camera pan/zoom or fast head turn raises
motion everywhere. Music or noise over silence looks like speech. A face turned away, covered
or smaller than about 25 px at 640 px frame width gives weak motion. A speaker off screen is
invisible. Without an audio stream the result uses motion only and says so ("audio": false).
Time resolution is about 0.25 s (the track sample spacing).
"""
from __future__ import annotations

import numpy as np

from . import _media, cache

VERSION = 1
ANALYSIS_FPS = 12.0
FRAME_WIDTH = 640


def mouth_box(box):
    """Lower third of a normalised face box, narrowed to the central 60%."""
    x, y, w, h = box
    return (x + 0.2 * w, y + 2 * h / 3, 0.6 * w, h / 3)


def upper_box(box):
    x, y, w, h = box
    return (x + 0.2 * w, y, 0.6 * w, h / 2)


def _region(frame, box):
    fh, fw = frame.shape[:2]
    x0, y0 = int(max(0, box[0] * fw)), int(max(0, box[1] * fh))
    x1, y1 = int(min(fw, np.ceil((box[0] + box[2]) * fw))), int(min(fh, np.ceil((box[1] + box[3]) * fh)))
    return (slice(y0, y1), slice(x0, x1)) if x1 - x0 >= 2 and y1 - y0 >= 2 else None


def _energy(prev, cur, box) -> float | None:
    sl = _region(cur, box)
    if sl is None:
        return None
    return float(np.abs(cur[sl].astype(np.float32) - prev[sl].astype(np.float32)).mean())


def mouth_motion(prev, cur, face_box) -> float | None:
    """Head-motion-compensated mouth energy for one frame pair (gray uint8 frames)."""
    mouth, upper = _energy(prev, cur, mouth_box(face_box)), _energy(prev, cur, upper_box(face_box))
    if mouth is None:
        return None
    return max(0.0, mouth - 0.5 * (upper or 0.0))


def _box_at(samples, t, tolerance):
    best = min(samples, key=lambda s: abs(s["t"] - t))
    if abs(best["t"] - t) > tolerance:
        return None
    return (best["x"], best["y"], best["w"], best["h"])


def audio_envelope(samples, rate, frame_times, start) -> np.ndarray:
    """RMS of the audio window centred on each frame time."""
    half = int(rate / ANALYSIS_FPS / 2)
    out = np.zeros(len(frame_times), np.float32)
    for i, t in enumerate(frame_times):
        centre = int((t - start) * rate)
        window = samples[max(0, centre - half):centre + half]
        out[i] = float(np.sqrt(np.mean(window ** 2))) if len(window) else 0.0
    return out


def _smooth(series, width=5):
    kernel = np.ones(width) / width
    padded = np.pad(series, width // 2, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def combine(frame_times, motion, envelope, track_samples, sample_fps):
    """motion: {track_id: array (nan = unknown)}; envelope: array or None -> per-track samples."""
    pooled = np.concatenate([m[~np.isnan(m)] for m in motion.values()] or [np.zeros(0)])
    scale = float(np.percentile(pooled, 90)) if pooled.size else 0.0
    scale = max(scale, 2.0)  # below ~2 grey levels of mean difference is codec noise, not a mouth
    audio = None
    if envelope is not None:
        top = float(np.percentile(envelope, 95)) if len(envelope) else 0.0
        audio = np.clip(envelope / top, 0, 1) if top > 1e-4 else np.zeros_like(envelope)
        audio = np.where(audio < 0.12, 0.0, audio)  # below the noise floor: nobody is talking
    result = {}
    for tid, samples in track_samples.items():
        m = np.nan_to_num(motion[tid], nan=0.0)
        mn = np.clip(m / scale, 0, 1)
        weight = 1.0
        if audio is not None:
            raw = np.sqrt(mn * audio)
            if mn.std() > 1e-3 and audio.std() > 1e-3:
                weight = 0.6 + 0.4 * max(0.0, float(np.corrcoef(mn, audio)[0, 1]))
        else:
            raw = mn
        series = _smooth(raw * weight)
        radius = 0.5 / sample_fps
        rows = []
        for s in samples:
            near = np.abs(frame_times - s["t"]) <= max(radius, 0.5 / ANALYSIS_FPS)
            rows.append({"t": s["t"], "active": round(float(np.clip(series[near].mean() if near.any() else 0.0, 0, 1)), 3)})
        result[tid] = rows
    return result


def _times_and_motion(frames, tracks, tolerance):
    prev, times = None, []
    motion = {t["id"]: [] for t in tracks}
    for t, frame in frames:
        if prev is not None:
            times.append(t)
            for tr in tracks:
                box = _box_at(tr["samples"], t, tolerance)
                value = mouth_motion(prev, frame, box) if box else None
                motion[tr["id"]].append(np.nan if value is None else value)
        prev = frame
    return np.array(times), {k: np.array(v, np.float32) for k, v in motion.items()}


def from_frames(frames, face_tracks, *, envelope_fn=None, sample_fps=None) -> dict:
    """Core of `activity` on an iterable of (t, gray frame); `envelope_fn(times)` gives audio RMS or None."""
    tracks = face_tracks.get("tracks", [])
    sample_fps = sample_fps or face_tracks.get("sample_fps", 4.0)
    tolerance = 1.0 / sample_fps
    times, motion = _times_and_motion(frames, [t for t in tracks if t["samples"]], tolerance)
    if len(times) == 0:
        return {"version": VERSION, "audio": False,
                "per_track": {t["id"]: [{"t": s["t"], "active": 0.0} for s in t["samples"]] for t in tracks}}
    envelope = envelope_fn(times) if envelope_fn else None
    per_track = combine(times, motion, envelope, {t["id"]: t["samples"] for t in tracks if t["samples"]}, sample_fps)
    for t in tracks:
        per_track.setdefault(t["id"], [])
    return {"version": VERSION, "audio": envelope is not None, "per_track": per_track}


def activity(source_path, face_tracks, *, start=0, end=None, cache_dir=None) -> dict:
    """Per-track speaking activity, 0..1 per face sample, over [start, end)."""
    info = _media.probe(source_path)
    start, end = _media.span(info, start, end)
    sig = [[t["id"], len(t["samples"]), t["samples"][0]["t"] if t["samples"] else 0,
            t["samples"][-1]["t"] if t["samples"] else 0] for t in face_tracks.get("tracks", [])]
    params = {"start": round(start, 3), "end": round(end, 3), "tracks": sig}
    cached = cache.load(cache_dir, source_path, "speech", VERSION, params)
    if cached is not None:
        return cached
    face_tracks = {**face_tracks, "tracks": [
        {**t, "samples": [s for s in t["samples"] if start <= s["t"] <= end]} for t in face_tracks.get("tracks", [])]}
    times = [s["t"] for t in face_tracks["tracks"] for s in t["samples"]]
    if not times:
        return {"version": VERSION, "audio": False,
                "per_track": {t["id"]: [] for t in face_tracks.get("tracks", [])}}
    lo, hi = max(start, min(times) - 0.5), min(end, max(times) + 0.5)
    rate = 16000
    samples = _media.audio(source_path, start=lo, end=hi, rate=rate)
    frames = _media.iter_frames(source_path, start=lo, end=hi, fps=ANALYSIS_FPS, width=FRAME_WIDTH)
    envelope_fn = None if samples is None else (lambda ts: audio_envelope(samples, rate, ts, lo))
    result = from_frames(frames, face_tracks, envelope_fn=envelope_fn)
    cache.store(cache_dir, source_path, "speech", VERSION, params, result)
    return result
