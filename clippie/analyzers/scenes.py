"""Hard-cut detection with FFmpeg's scene score on every decoded frame (exact cut times)."""
from __future__ import annotations

import re

from .. import media
from . import _media, cache

VERSION = 1
MIN_GAP = 0.25  # cuts closer than this are one flash, keep the first
EDGE = 0.1      # a "cut" this close to the range start is the first frame, not a cut


def cuts_from_log(stderr: str, start: float, end: float, min_gap: float = MIN_GAP) -> list[float]:
    times = []
    for match in re.finditer(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)", stderr):
        t = start + float(match.group(1))
        if start + EDGE < t < end - 0.02 and (not times or t - times[-1] >= min_gap):
            times.append(round(t, 3))
    return times


def detect(source_path, *, start=0, end=None, threshold=0.3, cache_dir=None) -> list[float]:
    """Cut times in source seconds, sorted. `threshold` is FFmpeg's 0..1 scene score."""
    info = _media.probe(source_path)
    start, end = _media.span(info, start, end)
    params = {"start": round(start, 3), "end": round(end, 3), "threshold": float(threshold)}
    cached = cache.load(cache_dir, source_path, "scenes", VERSION, params)
    if cached is not None:
        return list(cached)
    vf = f"scale=320:-2,select='gt(scene,{float(threshold):.4f})',showinfo"
    result = media._run(
        [media._binary("ffmpeg", "CLIPPIE_FFMPEG"), "-hide_banner", "-loglevel", "info", "-nostdin",
         "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source_path), "-an", "-sn",
         "-vf", vf, "-f", "null", "-"],
        timeout=int(120 + 2 * (end - start)), what="Scene cut analysis")
    cuts = cuts_from_log(result.stderr, start, end)
    cache.store(cache_dir, source_path, "scenes", VERSION, params, cuts)
    return cuts
