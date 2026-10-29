"""Canonical timeline validation (v1/v2) shared by the renderer and the timeline v3 conversion."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any



from .paths import resource_root

_STYLE_DIR = resource_root() / "styles"
_ROLES = {"hook", "context", "development", "payoff"}
_GENRE_GUIDE = {"documentary", "interview", "education", "travel", "review", "other"}
_ALLOWED_ASPECTS = {"16:9", "9:16"}
_MAX_DIMENSION = 3840
_STOP = frozenset("a an and are as at be been but by for from had has have he her hers him his i if in into is it its of on or our she that the their them then there they this to was we were what when where which who will with would you your".split())
def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric.")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite.")
    return number


def _source_duration(source_id: str, sources: dict[str, Any]) -> float:
    if source_id not in sources or not isinstance(sources[source_id], dict):
        raise ValueError(f"Unknown source_id {source_id!r}.")
    duration = _finite(sources[source_id].get("duration"), f"source {source_id!r} duration")
    if duration <= 0:
        raise ValueError(f"Source {source_id!r} has no usable duration.")
    return duration


def _framing(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("framing must be an object.")
    mode = value.get("mode", "fit")
    if not isinstance(mode, str) or mode not in {"fit", "fill"}:
        raise ValueError("framing.mode must be fit or fill.")
    zoom = _finite(value.get("zoom", 1.0), "framing.zoom")
    x = _finite(value.get("x", 0.5), "framing.x")
    y = _finite(value.get("y", 0.5), "framing.y")
    if not 1.0 <= zoom <= 1.5:
        raise ValueError("framing.zoom must be between 1.0 and 1.5.")
    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        raise ValueError("framing x and y must be between 0 and 1.")
    return {"mode": mode, "zoom": zoom, "x": x, "y": y}


def validate_timeline(timeline: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a minimal canonical timeline with program times."""
    if not isinstance(timeline, dict) or not isinstance(sources, dict):
        raise ValueError("timeline and sources must be objects.")
    version = timeline.get("schema_version", 1)
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("timeline schema_version must be integer 1 or 2.")
    aspect = timeline.get("aspect_ratio", "9:16" if version == 2 else "16:9")
    if not isinstance(aspect, str) or aspect not in _ALLOWED_ASPECTS:
        raise ValueError("aspect_ratio must be 16:9 or 9:16.")
    default_size = (1080, 1920) if version == 2 and aspect == "9:16" else (1280, 720)
    width, height = timeline.get("width", default_size[0]), timeline.get("height", default_size[1])
    if isinstance(width, bool) or not isinstance(width, int) or isinstance(height, bool) or not isinstance(height, int):
        raise ValueError("width and height must be integers.")
    if width < 240 or height < 240 or width > _MAX_DIMENSION or height > _MAX_DIMENSION or width % 2 or height % 2:
        raise ValueError("width and height must be even values from 240 through 3840.")
    ratio = width / height
    wanted = 16 / 9 if aspect == "16:9" else 9 / 16
    if abs(ratio - wanted) > 0.005:
        raise ValueError("aspect_ratio does not match width and height.")
    fps = timeline.get("fps", 30)
    if isinstance(fps, bool) or not isinstance(fps, int) or fps not in {24, 25, 30}:
        raise ValueError("fps must be 24, 25, or 30.")

    clips_in = timeline.get("clips")
    minimum_clips = 1 if version == 2 else 2
    if not isinstance(clips_in, list) or len(clips_in) < minimum_clips:
        raise ValueError("A timeline needs at least one primary clip." if version == 2
                         else "A timeline needs at least two primary clips.")
    ids: set[str] = set()
    clips: list[dict[str, Any]] = []
    source_ranges: dict[str, list[tuple[float, float]]] = {}
    program_at = 0.0
    for index, item in enumerate(clips_in, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"clip {index} must be an object.")
        clip_id = item.get("id")
        if not isinstance(clip_id, str) or not clip_id.strip() or clip_id in ids:
            raise ValueError("Every clip must have a unique non-empty ID.")
        ids.add(clip_id)
        source_id = item.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError(f"clip {clip_id!r} must reference a source_id.")
        source_duration = _source_duration(source_id, sources)
        start = _finite(item.get("start"), f"clip {clip_id!r} start")
        end = _finite(item.get("end"), f"clip {clip_id!r} end")
        if start < 0 or end <= start or end > source_duration + 1e-6:
            raise ValueError(f"clip {clip_id!r} range must be inside source {source_id!r} duration.")
        role = item.get("role")
        if version == 1 and (not isinstance(role, str) or role not in _ROLES):
            raise ValueError(f"clip {clip_id!r} role must be hook, context, development, or payoff.")
        if version == 2 and "role" in item and (not isinstance(role, str) or not role.strip() or len(role) > 80):
            raise ValueError(f"clip {clip_id!r} role must be non-empty text of at most 80 characters.")
        ranges = source_ranges.setdefault(source_id, [])
        if any(start < previous_end and previous_start < end for previous_start, previous_end in ranges):
            raise ValueError(f"clip {clip_id!r} overlaps another speech excerpt from source {source_id!r}.")
        ranges.append((start, end))
        note = item.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"clip {clip_id!r} note must be text.")
        framing = _framing(item.get("framing", {"mode": "fit", "zoom": 1, "x": 0.5, "y": 0.5}))
        duration = end - start
        clips.append({"id": clip_id, "source_id": source_id, "start": start, "end": end,
                      "role": role, "note": note, "framing": framing, "at": program_at})
        if version == 2 and "role" not in item:
            del clips[-1]["role"]
        program_at += duration
    if program_at > 180.0 + 1e-6:
        raise ValueError("Primary program duration cannot exceed 180 seconds.")

    broll_in = timeline.get("broll", [])
    if not isinstance(broll_in, list):
        raise ValueError("broll must be an array.")
    broll: list[dict[str, Any]] = []
    broll_intervals: list[tuple[float, float]] = []
    for index, item in enumerate(broll_in, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"broll item {index} must be an object.")
        broll_id = item.get("id")
        if not isinstance(broll_id, str) or not broll_id.strip() or broll_id in ids:
            raise ValueError("Every broll item must have a unique non-empty ID.")
        ids.add(broll_id)
        source_id = item.get("source_id")
        if not isinstance(source_id, str):
            raise ValueError(f"broll item {broll_id!r} must reference a source_id.")
        source_duration = _source_duration(source_id, sources)
        start = _finite(item.get("start"), f"broll {broll_id!r} start")
        end = _finite(item.get("end"), f"broll {broll_id!r} end")
        at = _finite(item.get("at"), f"broll {broll_id!r} at")
        duration = end - start
        if start < 0 or duration <= 0 or end > source_duration + 1e-6:
            raise ValueError(f"broll {broll_id!r} range must be inside source {source_id!r} duration.")
        if at < 0 or at + duration > program_at + 1e-6:
            raise ValueError(f"broll {broll_id!r} must fit inside the primary program duration.")
        if any(at < old_end and old_start < at + duration for old_start, old_end in broll_intervals):
            raise ValueError("B-roll intervals cannot overlap one another.")
        broll_intervals.append((at, at + duration))
        note = item.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"broll {broll_id!r} note must be text.")
        framing = _framing(item.get("framing", {"mode": "fit", "zoom": 1, "x": 0.5, "y": 0.5}))
        broll.append({"id": broll_id, "source_id": source_id, "start": start, "end": end,
                      "at": at, "note": note, "framing": framing})

    titles_in = timeline.get("titles", [])
    if not isinstance(titles_in, list):
        raise ValueError("titles must be an array.")
    titles = []
    title_intervals: list[tuple[float, float]] = []
    for index, item in enumerate(titles_in, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise ValueError(f"title {index} must have non-empty text.")
        at = _finite(item.get("at"), f"title {index} at")
        duration = _finite(item.get("duration"), f"title {index} duration")
        if at < 0 or duration <= 0 or at + duration > program_at + 1e-6:
            raise ValueError(f"title {index} must fit inside the primary program duration.")
        if any(at < old_end and old_start < at + duration for old_start, old_end in title_intervals):
            raise ValueError("Title overlays cannot overlap one another.")
        title_intervals.append((at, at + duration))
        titles.append({"text": item["text"].strip(), "at": at, "duration": duration})

    captions = timeline.get("captions", True)
    if not isinstance(captions, bool):
        raise ValueError("captions must be boolean.")
    caption_style = timeline.get("caption_style", "clean_focus")
    if not isinstance(caption_style, str) or caption_style not in {"clean_focus", "editorial", "phrase"}:
        raise ValueError("caption_style must be clean_focus, editorial or phrase.")
    canvas_style = timeline.get("canvas_style", "plain")
    if not isinstance(canvas_style, str) or canvas_style not in {"plain", "blurred"}:
        raise ValueError("canvas_style must be plain or blurred.")
    audio_in = timeline.get("audio", {"normalize": True})
    if not isinstance(audio_in, dict) or not isinstance(audio_in.get("normalize", True), bool):
        raise ValueError("audio.normalize must be boolean.")

    return {
        "schema_version": version, "aspect_ratio": aspect, "width": width, "height": height, "fps": fps,
        "clips": clips, "broll": broll, "titles": titles, "captions": captions,
        "caption_style": caption_style, "audio": {"normalize": audio_in.get("normalize", True)},
        "duration": program_at,
        **({"canvas_style": canvas_style} if version == 2 or "canvas_style" in timeline else {}),
    }
