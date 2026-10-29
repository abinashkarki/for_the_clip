"""Timeline v3: validation, program maps and v2 conversion (docs/v3/TIMELINE_V3.md).

`validate` returns a canonical deep copy: defaults filled, shots in canonical form (one `fit` shot when
absent, boundaries snapped), unknown top-level fields dropped, unknown caption word keys dropped. Optional
fields take these defaults: `captions` enabled with `bold_pop`; hook `style` `hook_box`; progress bar
`#FFD400`/`bottom`/12 px; logo `top_right`/0.14/0.9; keyframe `x`,`y` 0.5 and `zoom` 1; cut `reason` `manual`.
"""

from __future__ import annotations

import bisect
import copy
import math
import re
from typing import Any

from . import caption_templates

MIN_CUT = 0.04
MIN_DURATION = 0.5
MAX_DURATION = 180.0
MIN_BROLL = 0.3
SNAP = 1e-3
EPS = 1e-6
ASPECTS = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:5": (4, 5)}
DEFAULT_CANVAS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
LAYOUT_REGIONS = {"fill": 1, "fit": 1, "stack": 2, "screen_speaker": 2}
# Region rectangles as (x, y, w, h) fractions of the canvas, and how the region draws the source.
REGION_RECTS = {
    "fill": [(0, 0, 1, 1)], "fit": [(0, 0, 1, 1)],
    "stack": [(0, 0, 1, 0.5), (0, 0.5, 1, 0.5)],
    "screen_speaker": [(0, 0, 1, 0.6), (0, 0.6, 1, 0.4)],
}
REGION_MODES = {"fill": ["crop"], "fit": ["fit"], "stack": ["crop", "crop"], "screen_speaker": ["fit", "crop"]}
CUT_REASONS = ("filler", "silence", "manual")
V2_TEMPLATES = {"clean_focus": "boxed_word", "editorial": "color_word", "phrase": "phrase_clean"}
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ID_MAX = 64
# A cold open replays a short phrase from later in the clip before it starts; only the first segment may be one.
TEASER_MAX = 6.0
TEASER_KEY_SUFFIX = "#cold"


class TimelineError(ValueError):
    """Invalid timeline; `.path` locates the offending field, e.g. `segments[0].cuts[1]`."""

    def __init__(self, message: str, path: str = ""):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


def _fail(path: str, message: str):
    raise TimelineError(message, path)


def _num(value: Any, path: str, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(path, "must be a finite number")
    if (low is not None and value < low) or (high is not None and value > high):
        _fail(path, f"must be from {low} to {high}")
    return float(value)


def _obj(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    return value


def _list(value: Any, path: str) -> list:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    return value


def _id(item: dict, path: str, seen: set[str]) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value.strip() or len(value) > ID_MAX:
        _fail(f"{path}.id", f"must be text of 1-{ID_MAX} characters")
    if value in seen:
        _fail(f"{path}.id", f"duplicate id {value!r}")
    seen.add(value)
    return value


def _color(value: Any, path: str) -> str:
    if not isinstance(value, str) or not COLOR_RE.match(value):
        _fail(path, "must be #RRGGBB")
    return value.upper()


def _source_duration(sources: dict, source_id: Any, path: str) -> float:
    if not isinstance(source_id, str) or source_id not in sources:
        _fail(path, f"unknown source {source_id!r}")
    duration = sources[source_id].get("duration") if isinstance(sources[source_id], dict) else None
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not duration > 0:
        _fail(path, f"source {source_id!r} has no usable duration")
    return float(duration)


# ------------------------------------------------------------------ canvas

def _canvas(raw: Any) -> dict:
    raw = _obj(raw if raw is not None else {}, "canvas")
    width, height = raw.get("width", 1080), raw.get("height", 1920)
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or not 240 <= value <= 3840 or value % 2:
            _fail(f"canvas.{name}", "must be an even integer from 240 to 3840")
    if not any(width * b == height * a for a, b in ASPECTS.values()):
        _fail("canvas", "aspect must be exactly 9:16, 16:9, 1:1 or 4:5")
    fps = raw.get("fps", 30)
    if isinstance(fps, bool) or fps not in (24, 25, 30):
        _fail("canvas.fps", "must be 24, 25 or 30")
    background = _obj(raw.get("background", {"kind": "blur", "color": "#000000"}), "canvas.background")
    kind = background.get("kind", "blur")
    if kind not in ("blur", "color"):
        _fail("canvas.background.kind", "must be blur or color")
    color = _color(background.get("color", "#000000"), "canvas.background.color")
    return {"width": width, "height": height, "fps": int(fps), "background": {"kind": kind, "color": color}}


def aspect_of(canvas: dict) -> str:
    return next(name for name, (a, b) in ASPECTS.items() if canvas["width"] * b == canvas["height"] * a)


# ------------------------------------------------------------------ segments

def _cuts(raw: Any, path: str, start: float, end: float) -> list[dict]:
    cuts = []
    previous_end = start
    for i, item in enumerate(_list(raw if raw is not None else [], path)):
        p = f"{path}[{i}]"
        item = _obj(item, p)
        a, b = _num(item.get("start"), f"{p}.start"), _num(item.get("end"), f"{p}.end")
        reason = item.get("reason", "manual")
        if reason not in CUT_REASONS:
            _fail(f"{p}.reason", f"must be one of {', '.join(CUT_REASONS)}")
        if b - a < MIN_CUT - 1e-9:
            _fail(p, f"must be at least {MIN_CUT} s long")
        if not start < a or not b < end:
            _fail(p, "must lie strictly inside its segment")
        if a < previous_end - 1e-9 or (cuts and a < cuts[-1]["end"]):
            _fail(p, "must be sorted and must not overlap the previous cut")
        previous_end = b
        cuts.append({"start": a, "end": b, "reason": reason})
    return cuts


def _segments(raw: Any, sources: dict) -> list[dict]:
    items = _list(raw, "segments")
    if not items:
        _fail("segments", "needs at least one segment")
    seen: set[str] = set()
    by_source: dict[str, list[tuple[float, float]]] = {}
    segments = []
    for i, item in enumerate(items):
        path = f"segments[{i}]"
        item = _obj(item, path)
        sid = _id(item, path, seen)
        source_id = item.get("source_id")
        duration = _source_duration(sources, source_id, f"{path}.source_id")
        start, end = _num(item.get("start"), f"{path}.start"), _num(item.get("end"), f"{path}.end")
        if not 0 <= start < end <= duration + EPS:
            _fail(path, f"must satisfy 0 <= start < end <= source duration ({duration:g})")
        role = item.get("role")
        if role is not None:
            # A teaser repeats words that also play later, so it is the one segment allowed to overlap.
            if role != "teaser" or i != 0 or len(items) < 2:
                _fail(f"{path}.role", "only the first of several segments may be a teaser")
            if end - start > TEASER_MAX + EPS:
                _fail(path, f"a teaser lasts at most {TEASER_MAX:g} s")
        else:
            spans = by_source.setdefault(source_id, [])
            if any(start < b and a < end for a, b in spans):
                _fail(path, f"overlaps another segment from source {source_id!r}")
            spans.append((start, end))
        segments.append({"id": sid, "source_id": source_id, "start": start, "end": min(end, duration),
                         "cuts": _cuts(item.get("cuts"), f"{path}.cuts", start, end),
                         **({"role": role} if role else {})})
    return segments


def is_teaser(segment: dict) -> bool:
    return segment.get("role") == "teaser"


def kept_ranges(segment: dict) -> list[tuple[float, float]]:
    ranges, cursor = [], segment["start"]
    for cut in segment["cuts"]:
        if cut["start"] > cursor:
            ranges.append((cursor, cut["start"]))
        cursor = cut["end"]
    if segment["end"] > cursor:
        ranges.append((cursor, segment["end"]))
    return ranges


def program_map(timeline: dict) -> list[dict]:
    """Ordered kept ranges: `{source_id, src_start, src_end, at}` with `at` in program seconds.

    Ranges of a cold-open teaser segment also carry `teaser: true`.
    """
    result, at = [], 0.0
    for segment in timeline["segments"]:
        for a, b in kept_ranges(segment):
            result.append({"source_id": segment["source_id"], "src_start": a, "src_end": b, "at": at,
                           **({"teaser": True} if is_teaser(segment) else {})})
            at += b - a
    return result


def duration(timeline: dict) -> float:
    return math.fsum(r["src_end"] - r["src_start"] for r in program_map(timeline))


# ------------------------------------------------------------------ shots, b-roll

def interpolate(keyframes: list[dict], t: float) -> dict:
    """Linear interpolation of `x`,`y`,`zoom`; held before the first and after the last keyframe."""
    if t <= keyframes[0]["t"] or len(keyframes) == 1:
        first = keyframes[0]
        return {"x": first["x"], "y": first["y"], "zoom": first["zoom"]}
    if t >= keyframes[-1]["t"]:
        last = keyframes[-1]
        return {"x": last["x"], "y": last["y"], "zoom": last["zoom"]}
    i = bisect.bisect_right([k["t"] for k in keyframes], t) - 1
    a, b = keyframes[i], keyframes[i + 1]
    f = (t - a["t"]) / (b["t"] - a["t"])
    return {key: a[key] + (b[key] - a[key]) * f for key in ("x", "y", "zoom")}


def _framing(raw: Any, path: str) -> dict:
    raw = _obj(raw if raw is not None else {}, path)
    return {"x": _num(raw.get("x", 0.5), f"{path}.x", 0, 1), "y": _num(raw.get("y", 0.5), f"{path}.y", 0, 1),
            "zoom": _num(raw.get("zoom", 1.0), f"{path}.zoom", 1, 3)}


def _region(raw: Any, path: str, start: float, end: float, default_t: float) -> dict:
    raw = _obj(raw, path)
    if "source_id" in raw:
        _fail(f"{path}.source_id", "is reserved and must be absent")
    frames_in = raw.get("keyframes")
    if not isinstance(frames_in, list) or not frames_in:
        _fail(f"{path}.keyframes", "needs at least one keyframe")
    frames: list[dict] = []
    for i, frame in enumerate(frames_in):
        p = f"{path}.keyframes[{i}]"
        frame = _obj(frame, p)
        t = _num(frame.get("t", default_t if i == 0 else None), f"{p}.t")
        if not start - SNAP <= t <= end + SNAP:
            _fail(f"{p}.t", "must lie within the shot")
        if frames and t <= frames[-1]["t"]:
            _fail(f"{p}.t", "must be strictly increasing")
        frames.append({"t": t, **_framing(frame, p)})
    return {"keyframes": frames}


def _shots(raw: Any, total: float) -> list[dict]:
    items = _list(raw if raw is not None else [], "shots")
    if not items:
        return [{"id": "shot1", "start": 0.0, "end": total, "layout": "fit",
                 "regions": [{"keyframes": [{"t": 0.0, "x": 0.5, "y": 0.5, "zoom": 1.0}]}]}]
    seen: set[str] = set()
    shots: list[dict] = []
    cursor = 0.0
    for i, item in enumerate(items):
        path = f"shots[{i}]"
        item = _obj(item, path)
        sid = _id(item, path, seen)
        start, end = _num(item.get("start"), f"{path}.start"), _num(item.get("end"), f"{path}.end")
        if abs(start - cursor) > SNAP:
            _fail(f"{path}.start", "shots must be contiguous, the first starting at 0" if i == 0 else
                  f"must equal the previous shot end ({cursor:g}) within 1 ms")
        start = cursor
        if i == len(items) - 1:
            if abs(end - total) > SNAP:
                _fail(f"{path}.end", f"the last shot must end at the program duration ({total:g}) within 1 ms")
            end = total
        if not end - start > SNAP:
            _fail(path, "must have positive length")
        layout = item.get("layout")
        if layout not in LAYOUT_REGIONS:
            _fail(f"{path}.layout", f"must be one of {', '.join(LAYOUT_REGIONS)}")
        regions_in = item.get("regions")
        if regions_in is None:
            regions_in = [{"keyframes": [{"t": start}]} for _ in range(LAYOUT_REGIONS[layout])]
        regions_in = _list(regions_in, f"{path}.regions")
        if len(regions_in) != LAYOUT_REGIONS[layout]:
            _fail(f"{path}.regions", f"layout {layout} needs exactly {LAYOUT_REGIONS[layout]} region(s)")
        regions = [_region(r, f"{path}.regions[{j}]", start, end, start) for j, r in enumerate(regions_in)]
        shots.append({"id": sid, "start": start, "end": end, "layout": layout, "regions": regions})
        cursor = end
    return shots


def _broll(raw: Any, sources: dict, total: float) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    previous_end = 0.0
    for i, item in enumerate(_list(raw if raw is not None else [], "broll")):
        path = f"broll[{i}]"
        item = _obj(item, path)
        bid = _id(item, path, seen)
        source_id = item.get("source_id")
        source_duration = _source_duration(sources, source_id, f"{path}.source_id")
        start = _num(item.get("source_start"), f"{path}.source_start", 0)
        length = _num(item.get("duration"), f"{path}.duration", MIN_BROLL - 1e-9)
        at = _num(item.get("at"), f"{path}.at", 0)
        if start + length > source_duration + EPS:
            _fail(path, "source_start + duration exceeds the source duration")
        if at + length > total + EPS:
            _fail(path, "at + duration exceeds the program duration")
        if at < previous_end - 1e-9:
            _fail(path, "must be sorted by `at` and must not overlap the previous b-roll")
        previous_end = at + length
        result.append({"id": bid, "source_id": source_id, "source_start": start, "at": at, "duration": length,
                       "framing": _framing(item.get("framing"), f"{path}.framing")})
    return result


# ------------------------------------------------------------------ captions, overlays

def _word_keys(sources: dict) -> set[str]:
    keys = set()
    for sid, source in sources.items():
        for index, word in enumerate(source.get("words") or []):
            keys.add(f"{sid}:{word.get('id', index)}")
    return keys


def _captions(raw: Any, sources: dict) -> dict:
    raw = _obj(raw if raw is not None else {}, "captions")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        _fail("captions.enabled", "must be true or false")
    template_id = raw.get("template_id", "bold_pop")
    overrides = raw.get("overrides", {})
    try:
        caption_templates.resolve(template_id, overrides)
    except caption_templates.TemplateError as exc:
        _fail(f"captions.{exc.path}" if exc.path.startswith("overrides") or exc.path == "template_id" else
              f"captions.overrides.{exc.path}", str(exc).split(": ", 1)[-1])
    known = _word_keys(sources)
    lists = {}
    for name in ("emphasis", "hidden"):
        values = _list(raw.get(name, []), f"captions.{name}")
        lists[name] = list(dict.fromkeys(v for v in values if isinstance(v, str) and v in known))
    return {"enabled": enabled, "template_id": template_id, "overrides": copy.deepcopy(overrides), **lists}


def _overlays(raw: Any, total: float) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    hooks: list[tuple[float, float]] = []
    for i, item in enumerate(_list(raw if raw is not None else [], "overlays")):
        path = f"overlays[{i}]"
        item = _obj(item, path)
        oid = _id(item, path, seen)
        kind = item.get("kind")
        if kind == "hook":
            text = item.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 120:
                _fail(f"{path}.text", "must be 1-120 characters")
            start, end = _num(item.get("start"), f"{path}.start", 0), _num(item.get("end"), f"{path}.end")
            if not start < end <= total + EPS:
                _fail(path, "must satisfy 0 <= start < end <= program duration")
            if any(start < b and a < end for a, b in hooks):
                _fail(path, "hook intervals must not overlap")
            hooks.append((start, end))
            style = item.get("style", "hook_box")
            try:
                caption_templates.hook_style(style)
            except (caption_templates.TemplateError, TypeError):
                _fail(f"{path}.style", f"unknown hook style {style!r}")
            result.append({"id": oid, "kind": kind, "text": text.strip(), "start": start, "end": min(end, total),
                           "style": style})
        elif kind == "progress_bar":
            if any(o["kind"] == "progress_bar" for o in result):
                _fail(path, "at most one progress_bar overlay is allowed")
            position = item.get("position", "bottom")
            if position not in ("top", "bottom"):
                _fail(f"{path}.position", "must be top or bottom")
            result.append({"id": oid, "kind": kind, "color": _color(item.get("color", "#FFD400"), f"{path}.color"),
                           "position": position, "thickness": _num(item.get("thickness", 12), f"{path}.thickness", 4, 40)})
        elif kind == "logo":
            asset = item.get("asset_id")
            if not isinstance(asset, str) or not 1 <= len(asset) <= 100:
                _fail(f"{path}.asset_id", "must be text of 1-100 characters")
            anchor = item.get("anchor", "top_right")
            if anchor not in ("top_left", "top_right", "bottom_left", "bottom_right"):
                _fail(f"{path}.anchor", "must be top_left, top_right, bottom_left or bottom_right")
            result.append({"id": oid, "kind": kind, "asset_id": asset, "anchor": anchor,
                           "scale": _num(item.get("scale", 0.14), f"{path}.scale", 0.05, 0.4),
                           "opacity": _num(item.get("opacity", 0.9), f"{path}.opacity", 0.1, 1)})
        else:
            _fail(f"{path}.kind", "must be hook, progress_bar or logo")
    return result


# ------------------------------------------------------------------ public API

def validate(timeline: Any, sources: dict) -> dict:
    """Validate against the contract and return the canonical deep copy."""
    timeline = _obj(timeline, "timeline")
    if not isinstance(sources, dict):
        raise TimelineError("sources must be an object")
    if timeline.get("schema_version") != 3 or isinstance(timeline.get("schema_version"), bool):
        _fail("schema_version", "must be 3")
    canvas = _canvas(timeline.get("canvas"))
    segments = _segments(timeline.get("segments"), sources)
    canonical = {"schema_version": 3, "canvas": canvas, "segments": segments}
    total = duration(canonical)
    if total <= MIN_DURATION:
        _fail("segments", f"program duration must be more than {MIN_DURATION} s (got {total:.3f})")
    if total > MAX_DURATION + EPS:
        _fail("segments", f"program duration must be at most {MAX_DURATION:g} s (got {total:.3f})")
    canonical["shots"] = _shots(timeline.get("shots"), total)
    canonical["broll"] = _broll(timeline.get("broll"), sources, total)
    canonical["captions"] = _captions(timeline.get("captions"), sources)
    canonical["overlays"] = _overlays(timeline.get("overlays"), total)
    brand = timeline.get("brand_kit_id")
    if brand is not None and not isinstance(brand, str):
        _fail("brand_kit_id", "must be text or null")
    canonical["brand_kit_id"] = brand
    audio = _obj(timeline.get("audio", {"normalize": True}), "audio")
    normalize = audio.get("normalize", True)
    if not isinstance(normalize, bool):
        _fail("audio.normalize", "must be true or false")
    canonical["audio"] = {"normalize": normalize}
    return canonical


def program_words(timeline: dict, sources: dict) -> list[dict]:
    """Words in program time: `{key, text, start, end, source_id, word_id, emphasis, hidden}`."""
    emphasis, hidden = set(timeline["captions"]["emphasis"]), set(timeline["captions"]["hidden"])
    cache: dict[str, tuple[list, list]] = {}
    result = []
    for rng in program_map(timeline):
        sid = rng["source_id"]
        if sid not in cache:
            words = [(w, w.get("id", i)) for i, w in enumerate(sources[sid].get("words") or [])]
            words.sort(key=lambda pair: float(pair[0]["start"]))
            cache[sid] = ([float(w["start"]) for w, _ in words], words)
        starts, words = cache[sid]
        # any word starting before src_end can overlap; the midpoint test below decides.
        low = bisect.bisect_left(starts, rng["src_start"] - 30.0)
        for word, word_id in words[low:]:
            w_start, w_end = float(word["start"]), float(word["end"])
            if w_start >= rng["src_end"]:
                break
            if not rng["src_start"] <= (w_start + w_end) / 2 < rng["src_end"]:
                continue
            a, b = max(w_start, rng["src_start"]), min(w_end, rng["src_end"])
            base = f"{sid}:{word_id}"
            # A teaser replays words that also appear later; its copies get their own keys but share styling.
            key = base + (TEASER_KEY_SUFFIX if rng.get("teaser") else "")
            result.append({"key": key, "text": str(word.get("text", word.get("word", ""))).strip(),
                           "start": rng["at"] + a - rng["src_start"], "end": rng["at"] + b - rng["src_start"],
                           "source_id": sid, "word_id": word_id, "emphasis": base in emphasis, "hidden": base in hidden})
    result.sort(key=lambda w: (w["start"], w["end"]))
    return result


def default(source_id: str, start: float, end: float, sources: dict, *, aspect: str = "9:16") -> dict:
    if aspect not in DEFAULT_CANVAS:
        raise TimelineError(f"aspect must be one of {', '.join(DEFAULT_CANVAS)}", "aspect")
    width, height = DEFAULT_CANVAS[aspect]
    return validate({
        "schema_version": 3, "canvas": {"width": width, "height": height, "fps": 30,
                                        "background": {"kind": "blur", "color": "#000000"}},
        "segments": [{"id": "s1", "source_id": source_id, "start": start, "end": end, "cuts": []}],
        "shots": [], "broll": [], "captions": {"enabled": True, "template_id": "bold_pop"}, "overlays": [],
        "audio": {"normalize": True}}, sources)


# ------------------------------------------------------------------ v2 conversion

def _window_fraction(canvas_aspect: float, source_aspect: float, zoom: float) -> tuple[float, float]:
    """Crop window size as fractions of the source frame for a canvas-aspect window at `zoom`."""
    if source_aspect >= canvas_aspect:
        return canvas_aspect / source_aspect / zoom, 1 / zoom
    return 1 / zoom, source_aspect / canvas_aspect / zoom


def _v2_center(framing: dict, source: dict, canvas: dict) -> tuple[float, float]:
    """v2 x/y position the window within the slack (0 = left/top edge); v3 x/y is the window centre."""
    x, y, zoom = framing["x"], framing["y"], framing["zoom"]
    sw, sh = source.get("width"), source.get("height")
    if not (isinstance(sw, (int, float)) and isinstance(sh, (int, float)) and sw > 0 and sh > 0):
        return x, y
    cw, ch = _window_fraction(canvas["width"] / canvas["height"], sw / sh, zoom)
    return x * (1 - cw) + cw / 2, y * (1 - ch) + ch / 2


def from_v2(v2_timeline: dict, sources: dict) -> dict:
    """Convert a v1/v2 timeline (validated through `planner.validate_timeline`) to a validated v3 timeline."""
    from .planner import validate_timeline

    try:
        v2 = validate_timeline(v2_timeline, sources)
    except ValueError as exc:
        raise TimelineError(f"invalid v2 timeline: {exc}") from exc
    canvas = {"width": v2["width"], "height": v2["height"], "fps": v2["fps"],
              "background": {"kind": "blur" if v2.get("canvas_style") == "blurred" else "color", "color": "#000000"}}
    segments, shots, cursor = [], [], 0.0
    for index, clip in enumerate(v2["clips"], start=1):
        length = float(clip["end"]) - float(clip["start"])
        segments.append({"id": clip["id"] if len(clip["id"]) <= ID_MAX else f"s{index}", "source_id": clip["source_id"],
                         "start": float(clip["start"]), "end": float(clip["end"]), "cuts": []})
        framing = clip["framing"]
        if framing["mode"] == "fill":
            x, y = _v2_center(framing, sources[clip["source_id"]], canvas)
            frame = {"t": cursor, "x": x, "y": y, "zoom": framing["zoom"]}
            layout = "fill"
        else:
            frame = {"t": cursor, "x": 0.5, "y": 0.5, "zoom": framing["zoom"]}
            layout = "fit"
        shots.append({"id": f"shot{index}", "start": cursor, "end": cursor + length, "layout": layout,
                      "regions": [{"keyframes": [frame]}]})
        cursor += length
    broll = []
    for item in v2["broll"]:
        framing = item["framing"]
        if framing["mode"] == "fill":
            x, y = _v2_center(framing, sources[item["source_id"]], canvas)
        else:
            x, y = 0.5, 0.5
        broll.append({"id": item["id"], "source_id": item["source_id"], "source_start": float(item["start"]),
                      "at": float(item["at"]), "duration": float(item["end"]) - float(item["start"]),
                      "framing": {"x": x, "y": y, "zoom": framing["zoom"]}})
    overlays = [{"id": f"hook{i}", "kind": "hook", "text": title["text"][:120], "start": float(title["at"]),
                 "end": float(title["at"]) + float(title["duration"]), "style": "hook_box"}
                for i, title in enumerate(v2["titles"], start=1)]
    return validate({
        "schema_version": 3, "canvas": canvas, "segments": segments, "shots": shots, "broll": broll,
        "captions": {"enabled": bool(v2["captions"]), "template_id": V2_TEMPLATES[v2["caption_style"]]},
        "overlays": overlays, "brand_kit_id": None, "audio": {"normalize": v2["audio"]["normalize"]}}, sources)
