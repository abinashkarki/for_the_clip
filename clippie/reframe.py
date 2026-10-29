"""Auto-reframe: turn face, scene and speech evidence into timeline v3 `shots`.

Per program-time span between scene cuts (mapped back to the source through the program
map) the layout is chosen by rule: no face -> fit; one face or one dominant speaker -> fill
following the face; two visible faces -> stack; a small corner face over a static, detailed
frame -> screen_speaker. Keyframes follow the face centre with smoothing, a dead zone, a pan
speed limit and at most 2 keyframes per second, and keep the face in the upper-middle of the
window. A face box is never allowed outside its window; if that is impossible the span is fit.
Deterministic: no randomness, all times rounded to milliseconds.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .analyzers import AnalyzerUnavailable, _media, faces as faces_mod, scenes as scenes_mod, speech as speech_mod

try:  # stream B owns program_map; until it is importable the local copy below applies
    from . import timeline_v3 as _timeline_v3
except ImportError:  # pragma: no cover - exercised only before the streams are merged
    _timeline_v3 = None

MIN_SHOT = 0.25          # shorter scene runs are absorbed by their neighbour
MIN_KF_GAP = 0.5         # at most 1 keyframe per 0.5 s (so at most 2 per second)
SMOOTH_WINDOW = 1.0      # s, moving average on the target path
GRID = 0.25              # s, resolution of the target path
Y_ANCHOR = 0.38          # face centre sits at 38% of window height
MIN_FACE_H = 0.05        # smaller faces are background, not subjects
MIN_COVERAGE = 0.5       # share of a run's sample slots a face must appear in
STACK_COVERAGE = 0.6
DOMINANT = 0.8           # share of speech samples one speaker needs to win the whole run
FILL_FACE_FRACTION = 0.28
FILL_MAX_ZOOM = 1.6
SCREEN_FACE_H = 0.24
# (dead zone in window widths, max pan in window widths per second); looser plans are only
# tried when a tighter one would let a face box leave its window.
PLANS = [(0.10, 0.5), (0.04, 1.5), (0.02, 4.0)]


# ---- program map ------------------------------------------------------------------------

def kept_ranges(timeline: dict) -> list[dict]:
    """Local copy of timeline_v3.program_map: kept source ranges in program order."""
    out, at = [], 0.0
    for seg in timeline.get("segments", []):
        cursor = float(seg["start"])
        pieces = []
        for cut in sorted(seg.get("cuts") or [], key=lambda c: c["start"]):
            if cut["start"] > cursor:
                pieces.append((cursor, float(cut["start"])))
            cursor = max(cursor, float(cut["end"]))
        if float(seg["end"]) > cursor:
            pieces.append((cursor, float(seg["end"])))
        for lo, hi in pieces:
            out.append({"source_id": seg["source_id"], "src_start": lo, "src_end": hi, "at": at})
            at += hi - lo
    return out


def program_map(timeline: dict) -> list[dict]:
    if _timeline_v3 is not None:
        return _timeline_v3.program_map(timeline)
    return kept_ranges(timeline)


def _segment_index(timeline: dict, rng: dict) -> int:
    for i, seg in enumerate(timeline["segments"]):
        if (seg["source_id"] == rng["source_id"] and seg["start"] - 1e-6 <= rng["src_start"]
                and rng["src_end"] <= seg["end"] + 1e-6):
            return i
    return -1


# ---- geometry ---------------------------------------------------------------------------

def window_size(region_aspect: float, source_aspect: float, zoom: float) -> tuple[float, float]:
    """Crop window (width, height) as fractions of the source frame at `zoom` (1 = largest)."""
    return min(1.0, region_aspect / source_aspect) / zoom, min(1.0, source_aspect / region_aspect) / zoom


def clamp_centre(x, y, ww, wh):
    return min(max(x, ww / 2), 1 - ww / 2), min(max(y, wh / 2), 1 - wh / 2)


def interpolate(keyframes: list[dict], t: float):
    if t <= keyframes[0]["t"]:
        k = keyframes[0]
        return k["x"], k["y"]
    for a, b in zip(keyframes, keyframes[1:]):
        if t <= b["t"]:
            f = (t - a["t"]) / (b["t"] - a["t"])
            return a["x"] + (b["x"] - a["x"]) * f, a["y"] + (b["y"] - a["y"]) * f
    return keyframes[-1]["x"], keyframes[-1]["y"]


def _moving_average(values: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or len(values) < 2:
        return values
    width = min(width, len(values) * 2 - 1) | 1
    padded = np.pad(values, width // 2, mode="edge")
    return np.convolve(padded, np.ones(width) / width, mode="valid")


def _inside(box, cx, cy, ww, wh, tol=0.004) -> bool:
    x, y, w, h = box
    return (x >= cx - ww / 2 - tol and x + w <= cx + ww / 2 + tol
            and y >= cy - wh / 2 - tol and y + h <= cy + wh / 2 + tol)


def _kf(t, x, y, zoom):
    return {"t": round(t, 3), "x": round(x, 4), "y": round(y, 4), "zoom": round(zoom, 3)}


def plan_region(samples, p0, p1, *, region_aspect, source_aspect, face_fraction=FILL_FACE_FRACTION,
                max_zoom=FILL_MAX_ZOOM):
    """Keyframes keeping one face in a window, or None if it cannot fit at zoom 1.

    samples: [(p, x, y, w, h)] face boxes (normalised, top-left) at program times p in [p0, p1].
    """
    if not samples:
        return None
    arr = np.array(samples, float)
    ww1, _ = window_size(region_aspect, source_aspect, 1.0)
    zoom = min(max(face_fraction * ww1 / float(np.median(arr[:, 3])), 1.0), max_zoom)
    fw, fh = float(arr[:, 3].max()), float(arr[:, 4].max())
    while zoom > 1.0 and not all(a * 1.3 <= b for a, b in zip((fw, fh), window_size(region_aspect, source_aspect, zoom))):
        zoom = max(1.0, zoom - 0.05)
    ww, wh = window_size(region_aspect, source_aspect, zoom)
    if fw * 1.05 > ww or fh * 1.05 > wh:
        return None
    grid = np.arange(p0, p1, GRID)
    grid = np.append(grid, p1) if len(grid) == 0 or grid[-1] < p1 - 1e-9 else grid
    order = np.argsort(arr[:, 0], kind="stable")
    px = arr[order, 0]
    fx = np.interp(grid, px, arr[order, 1] + arr[order, 3] / 2)
    fy = np.interp(grid, px, arr[order, 2] + arr[order, 4] / 2)
    width = max(1, int(round(SMOOTH_WINDOW / GRID))) | 1
    tx, ty = _moving_average(fx, width), _moving_average(fy + (0.5 - Y_ANCHOR) * wh, width)
    tx = np.clip(tx, ww / 2, 1 - ww / 2)
    ty = np.clip(ty, wh / 2, 1 - wh / 2)
    for dead, pan in PLANS:
        keyframes = _follow(grid, tx, ty, ww, wh, zoom, p0, p1, dead, pan)
        if all(_inside((r[1], r[2], r[3], r[4]), *clamp_centre(*interpolate(keyframes, r[0]), ww, wh), ww, wh)
               for r in arr):
            return keyframes
    return None


def _follow(grid, tx, ty, ww, wh, zoom, p0, p1, dead, pan):
    def target(t):
        return float(np.interp(t, grid, tx)), float(np.interp(t, grid, ty))

    def dist(a, b):
        return max(abs(a[0] - b[0]) / ww, abs(a[1] - b[1]) / wh)

    pos = target(p0)
    keyframes = [_kf(p0, *pos, zoom)]
    last = p0
    for g in grid:
        if g <= last + 1e-9 or dist(target(g), pos) <= dead:
            continue
        # A face that keeps moving is followed continuously; after a pause the move gets
        # a hold keyframe so the window stays still until the face leaves the dead zone.
        hold = g - last >= 1.0
        start = max(g, last + MIN_KF_GAP) if hold else last
        if start + MIN_KF_GAP > p1 - 0.001:
            break
        goal, dur = target(start), MIN_KF_GAP
        for _ in range(3):
            goal = target(min(start + dur, p1))
            dur = min(3.0, max(MIN_KF_GAP, dist(goal, pos) / pan))
        end = start + dur
        if end > p1 - 0.001:
            end = p1 - 0.001
            frac = (end - start) / dur
            goal = (pos[0] + (goal[0] - pos[0]) * frac, pos[1] + (goal[1] - pos[1]) * frac)
        if hold:
            keyframes.append(_kf(start, *pos, zoom))
        keyframes.append(_kf(end, *goal, zoom))
        pos, last = goal, end
    return keyframes


# ---- screen / slide evidence ------------------------------------------------------------

def looks_like_screen(frames, face_box, *, static_std=3.0, min_static=0.9, edge_level=20.0, min_edges=0.003) -> bool:
    """True when the frame outside the face is almost static and carries slide-like detail.

    frames: gray uint8 arrays sampled across the run (>= 3). A static talking-head background
    has no edges, a slide or screen share has text and lines.
    """
    if len(frames) < 3:
        return False
    stack = np.stack([f.astype(np.float32) for f in frames])
    h, w = stack.shape[1:]
    mask = np.ones((h, w), bool)
    x, y, bw, bh = face_box
    mask[max(0, int((y - bh / 2) * h)):int(math.ceil((y + 1.5 * bh) * h)),
         max(0, int((x - bw / 2) * w)):int(math.ceil((x + 1.5 * bw) * w))] = False
    if mask.mean() < 0.5:
        return False
    static = float((stack.std(axis=0)[mask] < static_std).mean())
    gx = np.abs(np.diff(stack[0], axis=1))[mask[:, 1:]]
    gy = np.abs(np.diff(stack[0], axis=0))[mask[1:, :]]
    detail = (float((gx > edge_level).mean()) + float((gy > edge_level).mean())) / 2
    return static >= min_static and detail >= min_edges


def _sample_gray(source_path, s0, s1):
    dur = s1 - s0
    fps = min(2.0, max(0.5, 6.0 / dur))
    return [f for _, f in _media.iter_frames(source_path, start=s0, end=s1, fps=fps, width=320)]


# ---- evidence gathering -----------------------------------------------------------------

class _Chunk:
    def __init__(self, start, end, faces, scenes, speech=None, source_path=None, cache_dir=None):
        self.start, self.end, self.faces, self.scenes = start, end, faces, scenes
        self.speech, self.source_path, self.cache_dir = speech, source_path, cache_dir
        self._speech_tried = speech is not None

    def get_speech(self):
        if not self._speech_tried and self.source_path:
            self._speech_tried = True
            try:
                self.speech = speech_mod.activity(self.source_path, self.faces, start=self.start,
                                                  end=self.end, cache_dir=self.cache_dir)
            except (AnalyzerUnavailable, RuntimeError, ValueError, OSError):
                self.speech = None
        return self.speech


def _merged_ranges(ranges, gap=30.0):
    out = []
    for lo, hi in sorted(ranges):
        if out and lo - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _gather(timeline, sources, ranges, analysis, cache_dir):
    chunks = {}
    for sid in sorted({r["source_id"] for r in ranges}):
        src = sources.get(sid)
        if src is None:
            raise ValueError(f"Source {sid!r} is not in sources")
        path = src.get("source_path")
        given = (analysis or {}).get(sid) or {}
        cd = str(Path(cache_dir) / sid) if cache_dir else None
        if given.get("faces") is not None and given.get("scenes") is not None:
            chunks[sid] = [_Chunk(0.0, math.inf, given["faces"], list(given["scenes"]), given.get("speech"), path, cd)]
            continue
        if not path:
            raise ValueError(f"Source {sid!r} has no source_path and no analysis was supplied")
        spans = _merged_ranges([(s["start"], s["end"]) for s in timeline["segments"] if s["source_id"] == sid])
        chunks[sid] = []
        for lo, hi in spans:
            f = given.get("faces") or faces_mod.track(path, start=lo, end=hi, cache_dir=cd)
            c = given.get("scenes")
            c = list(c) if c is not None else scenes_mod.detect(path, start=lo, end=hi, cache_dir=cd)
            chunks[sid].append(_Chunk(lo, hi, f, c, given.get("speech"), path, cd))
    return chunks


def _chunk_for(chunks, sid, s0, s1):
    for c in chunks[sid]:
        if c.start - 1e-6 <= s0 and s1 <= c.end + 1e-6:
            return c
    return chunks[sid][0]


# ---- runs: contiguous picture between scene cuts ----------------------------------------

def _build_runs(timeline, ranges, chunks):
    """Split kept ranges at scene cuts; join pieces that continue the same scene into runs."""
    runs = []
    for rng in ranges:
        sid, s0, s1, at = rng["source_id"], rng["src_start"], rng["src_end"], rng["at"]
        seg = _segment_index(timeline, rng)
        chunk = _chunk_for(chunks, sid, s0, s1)
        cuts = sorted({c for ch in chunks[sid] for c in ch.scenes if s0 + 0.02 < c < s1 - 0.02})
        edges = [s0] + cuts + [s1]
        for k, (a, b) in enumerate(zip(edges, edges[1:])):
            piece = {"sid": sid, "s0": a, "s1": b, "p0": at + (a - s0), "p1": at + (b - s0), "chunk": chunk}
            hard = k > 0 or not runs or runs[-1]["seg"] != seg or runs[-1]["pieces"][-1]["sid"] != sid
            if hard:
                runs.append({"seg": seg, "pieces": [piece]})
            else:
                runs[-1]["pieces"].append(piece)
    # Absorb runs too short to frame into the run before them (or after, for the first).
    merged = []
    for run in runs:
        if merged and run["pieces"][-1]["p1"] - run["pieces"][0]["p0"] < MIN_SHOT:
            merged[-1]["pieces"].extend(run["pieces"])
        else:
            merged.append(run)
    while len(merged) > 1 and merged[0]["pieces"][-1]["p1"] - merged[0]["pieces"][0]["p0"] < MIN_SHOT:
        merged[1]["pieces"][:0] = merged[0]["pieces"]
        merged.pop(0)
    for run in merged:
        run["p0"], run["p1"] = run["pieces"][0]["p0"], run["pieces"][-1]["p1"]
    return merged


def _run_tracks(run, fps):
    """Face tracks restricted to a run, in program time: {key: {"samples": [...], "coverage": c}}."""
    tracks = {}
    for piece in run["pieces"]:
        for tr in piece["chunk"].faces.get("tracks", []):
            for s in tr["samples"]:
                if piece["s0"] - 1e-6 <= s["t"] < piece["s1"] + 1e-6:
                    key = (id(piece["chunk"]), tr["id"])
                    tracks.setdefault(key, {"chunk": piece["chunk"], "id": tr["id"], "samples": []})["samples"].append(
                        (piece["p0"] + s["t"] - piece["s0"], s["x"], s["y"], s["w"], s["h"], s["t"]))
    length = run["p1"] - run["p0"]
    for tr in tracks.values():
        tr["samples"].sort()
        tr["coverage"] = min(1.0, len(tr["samples"]) / max(1.0, length * fps))
        tr["median_h"] = float(np.median([s[4] for s in tr["samples"]]))
        tr["cx"] = float(np.median([s[1] + s[3] / 2 for s in tr["samples"]]))
        tr["cy"] = float(np.median([s[2] + s[4] / 2 for s in tr["samples"]]))
    return tracks


def _dominance(visible):
    """Share of speech samples each visible face wins, or None without speech evidence."""
    chunk = visible[0]["chunk"]
    result = chunk.get_speech()
    if not result or any(v["chunk"] is not chunk for v in visible):
        return None
    lookup = {v["id"]: {round(r["t"], 3): r["active"] for r in result["per_track"].get(v["id"], [])} for v in visible}
    times = sorted({round(s[5], 3) for v in visible for s in v["samples"]})
    wins, speaking = {v["id"]: 0 for v in visible}, 0
    for t in times:
        acts = sorted(((lookup[v["id"]].get(t, 0.0), v["id"]) for v in visible), reverse=True)
        top, second = acts[0][0], acts[1][0] if len(acts) > 1 else 0.0
        if top >= 0.25:
            speaking += 1
            if top - second >= 0.1:
                wins[acts[0][1]] += 1
    if speaking < 3:
        return None
    return {k: v / speaking for k, v in wins.items()}


# ---- decision ---------------------------------------------------------------------------

def _fit(t):
    return "fit", [[_kf(t, 0.5, 0.5, 1.0)]]


def _decide(run, p0, p1, ctx):
    fps = run["pieces"][0]["chunk"].faces.get("sample_fps", 4.0)
    tracks = _run_tracks(run, fps)
    visible = sorted((t for t in tracks.values() if t["coverage"] >= MIN_COVERAGE and t["median_h"] >= MIN_FACE_H),
                     key=lambda t: (t["cx"], t["id"]))
    sid = run["pieces"][0]["sid"]
    aspect = ctx["aspect"][sid]
    fill = lambda tr: plan_region([s[:5] for s in tr["samples"]], p0, p1, region_aspect=ctx["canvas_aspect"],
                                  source_aspect=aspect)
    if not visible:
        return _fit(p0)
    if len(visible) >= 2:
        share = _dominance(visible)
        if share:
            lead = max(visible, key=lambda t: share[t["id"]])
            if share[lead["id"]] > DOMINANT:
                visible = [lead]
    if len(visible) == 1:
        solo = visible[0]
        screen = _screen_speaker(run, solo, p0, p1, ctx, aspect)
        if screen:
            return screen
        keyframes = fill(solo)
        return ("fill", [keyframes]) if keyframes else _fit(p0)
    if len(visible) == 2 and all(t["coverage"] >= STACK_COVERAGE for t in visible):
        half = ctx["canvas"][0] / (ctx["canvas"][1] / 2)
        plans = [plan_region([s[:5] for s in t["samples"]], p0, p1, region_aspect=half, source_aspect=aspect)
                 for t in visible]
        if all(plans):
            return "stack", plans
        return _fit(p0)
    if len(visible) == 2:
        keyframes = fill(max(visible, key=lambda t: t["coverage"]))
        return ("fill", [keyframes]) if keyframes else _fit(p0)
    return _fit(p0)


def _screen_speaker(run, face, p0, p1, ctx, aspect):
    corner = abs(face["cx"] - 0.5) >= 0.2 and (face["cy"] <= 0.35 or face["cy"] >= 0.65)
    if face["median_h"] > SCREEN_FACE_H or not corner:
        return None
    piece = max(run["pieces"], key=lambda p: p["s1"] - p["s0"])
    sample = face["samples"][len(face["samples"]) // 2]
    try:
        frames = ctx["gray"](piece["sid"], piece["s0"], piece["s1"])
    except (RuntimeError, ValueError, OSError):
        return None
    if not looks_like_screen(frames, sample[1:5]):
        return None
    canvas_w, canvas_h = ctx["canvas"]
    speaker = plan_region([s[:5] for s in face["samples"]], p0, p1, region_aspect=canvas_w / (0.4 * canvas_h),
                          source_aspect=aspect, face_fraction=0.35, max_zoom=3.0)
    if not speaker:
        return None
    return "screen_speaker", [[_kf(p0, 0.5, 0.5, 1.0)], speaker]


# ---- public -----------------------------------------------------------------------------

def auto_shots(timeline, sources, *, analysis=None, cache_dir=None) -> list[dict]:
    """Timeline v3 `shots` covering [0, duration] for the timeline's segments.

    analysis: optional {source_id: {"faces", "scenes", "speech"}} (the analyzers' outputs);
    anything missing is computed from the source file (and cached under cache_dir/<source_id>).
    Raises AnalyzerUnavailable if face tracking is needed but unavailable.
    """
    ranges = program_map(timeline)
    if not ranges:
        raise ValueError("Timeline has no kept ranges")
    duration = ranges[-1]["at"] + ranges[-1]["src_end"] - ranges[-1]["src_start"]
    chunks = _gather(timeline, sources, ranges, analysis, cache_dir)
    canvas = timeline.get("canvas") or {}
    canvas_w, canvas_h = float(canvas.get("width", 1080)), float(canvas.get("height", 1920))
    aspect = {}
    for sid, items in chunks.items():
        f = items[0].faces
        w, h = f.get("width") or sources[sid].get("width"), f.get("height") or sources[sid].get("height")
        if not (w and h):
            info = _media.probe(sources[sid]["source_path"])
            w, h = info["width"], info["height"]
        aspect[sid] = w / h
    ctx = {"aspect": aspect, "canvas_aspect": canvas_w / canvas_h, "canvas": (canvas_w, canvas_h),
           "gray": lambda sid, a, b: _sample_gray(sources[sid]["source_path"], a, b)}
    runs = _build_runs(timeline, ranges, chunks)
    bounds = [0.0] + [round(r["p0"], 3) for r in runs[1:]] + [round(duration, 3)]
    shots = []
    for i, run in enumerate(runs):
        p0, p1 = bounds[i], bounds[i + 1]
        layout, regions = _decide(run, p0, p1, ctx)
        if shots and layout == "fit" and shots[-1]["layout"] == "fit":
            shots[-1]["end"] = p1
            continue
        shots.append({"id": "", "start": p0, "end": p1, "layout": layout,
                      "regions": [{"keyframes": kf} for kf in regions]})
    for n, shot in enumerate(shots, 1):
        shot["id"] = f"sh{n}"
    return shots
