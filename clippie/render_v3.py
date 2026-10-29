"""Timeline v3 renderer: layouts with keyframed framing, crossfaded audio, overlay layer, H.264 + AAC MP4.

Pipeline (three FFmpeg stages, each bounded by a timeout):

1. Audio: kept ranges are decoded separately and joined with 12 ms `acrossfade` overlaps (each range carries a
   handle of the same length so program time is preserved), then loudness-normalised in two passes.
2. Overlay frames: `Compositor.frame_plan` yields only the frames where captions, hook or logo change or animate;
   each distinct scene becomes one PNG and a concat-demuxer list gives it its duration.
3. Picture + encode: the program is cut into pieces (kept range x shot x b-roll) with constant layout, one input
   per piece (`-ss/-t`). Static framing uses `scale+crop`; panning uses `crop` with per-frame x/y expressions;
   zoom changes use `scale eval=frame` plus an `overlay` whose position follows the expression (crop's size is
   fixed per instance). The progress bar is an FFmpeg `overlay` of a solid strip moved by expression.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from . import compositor as comp
from . import timeline_v3 as tl
from .render import _binary, _concat_quote, _fmt_time, _notify, _run, process_ownership

CROSSFADE = 0.012
LOUDNESS = {"I": -14.0, "TP": -1.0, "LRA": 11.0}
PREVIEW_SHORT_SIDE = 540
BLUR_DIVISOR = 8
FRAME_MARGIN = 0.3  # extra seconds decoded per piece input


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


# ------------------------------------------------------------------ probing

def _probe(path: Path) -> dict:
    result = _run([_binary("ffprobe", "CLIPPIE_FFPROBE"), "-v", "error", "-show_streams", "-show_format", "-of", "json",
                   str(path)], timeout=30, what="Source probe")
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video")
        width, height = int(video["width"]), int(video["height"])
        duration = float(video.get("duration") or data.get("format", {}).get("duration"))
    except (ValueError, TypeError, StopIteration, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read usable video metadata from {path}.") from exc
    from .media import display_dimensions
    width, height = display_dimensions(video)
    return {"duration": duration, "width": width, "height": height, "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def _source_path(sources: dict, source_id: str) -> Path:
    raw = sources[source_id].get("source_path")
    if not raw:
        raise ValueError(f"Source {source_id!r} has no source_path.")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source {source_id!r} media does not exist: {path}")
    return path


# ------------------------------------------------------------------ piece plan

def output_size(canvas: dict, preview: bool) -> tuple[int, int]:
    if not preview:
        return canvas["width"], canvas["height"]
    scale = PREVIEW_SHORT_SIDE / min(canvas["width"], canvas["height"])
    return _even(canvas["width"] * scale), _even(canvas["height"] * scale)


def _frame(t: float, fps: int) -> int:
    return int(round(t * fps))


def plan_pieces(timeline: dict, fps: int) -> list[dict]:
    """Cut the program into constant-layout pieces with exact frame counts."""
    ranges = tl.program_map(timeline)
    for r in ranges:
        r["len"] = r["src_end"] - r["src_start"]
    total = tl.duration(timeline)
    shots, broll = timeline["shots"], timeline["broll"]
    points = {0.0, total}
    points.update(r["at"] for r in ranges)
    for shot in shots:
        points.update((shot["start"], shot["end"]))
    for item in broll:
        points.update((item["at"], item["at"] + item["duration"]))
    frames = sorted({min(_frame(total, fps), _frame(p, fps)) for p in points if 0 <= p <= total + 1e-9})
    pieces = []
    for first, last in zip(frames, frames[1:]):
        t0, mid = first / fps, (first + last) / (2 * fps)
        rng = next((r for r in ranges if r["at"] - 1e-9 <= mid < r["at"] + r["len"] + 1e-9), ranges[-1])
        shot = next((s for s in shots if s["start"] - 1e-9 <= mid < s["end"] + 1e-9), shots[-1])
        item = next((b for b in broll if b["at"] - 1e-9 <= mid < b["at"] + b["duration"]), None)
        if item:
            piece = {"kind": "broll", "source_id": item["source_id"], "layout": "fill", "shot_id": item["id"],
                     "src_start": item["source_start"] + (t0 - item["at"]),
                     "regions": [[{"t": t0, **item["framing"]}]]}
        else:
            piece = {"kind": "speech", "source_id": rng["source_id"], "layout": shot["layout"], "shot_id": shot["id"],
                     "src_start": max(rng["src_start"], rng["src_start"] + (t0 - rng["at"])),
                     "regions": [r["keyframes"] for r in shot["regions"]]}
        piece.update({"first_frame": first, "frames": last - first, "t0": t0})
        pieces.append(piece)
    return pieces


# ------------------------------------------------------------------ framing math and expressions

def region_rects(layout: str, width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Integer even-edged `(x, y, w, h)` rectangles for the layout's regions."""
    rects = []
    for fx, fy, fw, fh in tl.REGION_RECTS[layout]:
        top, bottom = _even(fy * height) if fy else 0, _even((fy + fh) * height) if fy + fh < 1 else height
        rects.append((0, top, width, bottom - top))
    return rects


def _expr(frames: list[dict], key: str, offset: float) -> str:
    """Piecewise-linear FFmpeg expression of `t` (piece-local seconds) for keyframe property `key`."""
    pts = [(f["t"] - offset, f[key]) for f in frames]
    out = _num(pts[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        slope = (v1 - v0) / (t1 - t0)
        out = f"if(lt(t,{_num(t1)}),{_num(v0)}+({_num(slope)})*(t-{_num(t0)}),{out})"
    return f"if(lt(t,{_num(pts[0][0])}),{_num(pts[0][1])},{out})" if len(pts) > 1 else _num(pts[0][1])


def motion_kind(frames: list[dict], t0: float, span: float) -> str:
    """`static`, `pan` (x/y change, zoom constant) or `zoom` over the piece interval."""
    samples = [tl.interpolate(frames, t0), tl.interpolate(frames, t0 + span)]
    samples += [tl.interpolate(frames, f["t"]) for f in frames if t0 < f["t"] < t0 + span]
    def varies(key: str) -> bool:
        values = [s[key] for s in samples]
        return max(values) - min(values) > 1e-4
    if varies("zoom"):
        return "zoom"
    return "pan" if varies("x") or varies("y") else "static"


def _crop_scale(src_w: float, src_h: float, rw: int, rh: int) -> tuple[float, float]:
    """(base window width in source px, scale factor at zoom 1) for a crop region."""
    base = min(src_w, src_h * rw / rh)
    return base, rw / base


def _clip_expr(value: str, high: str) -> str:
    return f"clip({value},0,{high})"


class _Graph:
    """Collects filter chains and hands out unique labels."""

    def __init__(self):
        self.lines: list[str] = []
        self.count = 0

    def label(self, prefix: str = "t") -> str:
        self.count += 1
        return f"{prefix}{self.count}"

    def add(self, source: str | list[str], chain: str, out: str | None = None) -> str:
        out = out or self.label()
        sources = [source] if isinstance(source, str) else source
        self.lines.append("".join(f"[{s}]" for s in sources) + chain + f"[{out}]")
        return out


def _bg_color(g: _Graph, width: int, height: int, fps: int, color: str, frames: int) -> str:
    return g.add([], f"color=c=0x{color.lstrip('#')}:s={width}x{height}:r={fps}:d={_num(frames / fps + 0.5)}")


def _bg_blur(g: _Graph, source: str, width: int, height: int) -> str:
    sw, sh = _even(width / BLUR_DIVISOR), _even(height / BLUR_DIVISOR)
    return g.add(source, f"scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},boxblur=5:2,"
                         f"eq=brightness=-0.1:saturation=1.15,scale={width}:{height}:flags=bilinear,setsar=1")


def _region_stream(g: _Graph, src: str, mode: str, rect: tuple, frames: list[dict], piece: dict, src_dims: tuple,
                   fps: int) -> tuple[str, dict]:
    """Build the stream for one region. Returns (label, placement) where placement is the overlay x/y onto the canvas."""
    rx, ry, rw, rh = rect
    src_w, src_h = src_dims
    span = piece["frames"] / fps
    t0 = piece["t0"]
    kind = motion_kind(frames, t0, span)
    start = tl.interpolate(frames, t0)
    if mode == "fit":
        factor0 = min(rw / src_w, rh / src_h)
        if kind != "zoom":
            w, h = _even(src_w * factor0 * start["zoom"]), _even(src_h * factor0 * start["zoom"])
            label = g.add(src, f"scale={w}:{h}:flags=bicubic,setsar=1")
            return label, {"x": f"{rx + (rw - w) // 2}", "y": f"{ry + (rh - h) // 2}"}
        z = _expr(frames, "zoom", t0)
        label = g.add(src, f"scale=w='2*ceil({_num(src_w * factor0)}*({z})/2)':h='2*ceil({_num(src_h * factor0)}*({z})/2)':"
                           "eval=frame:flags=bicubic,setsar=1")
        return label, {"x": f"{rx}+({rw}-w)/2", "y": f"{ry}+({rh}-h)/2"}
    base_w, factor = _crop_scale(src_w, src_h, rw, rh)
    if kind == "zoom":
        z, cx, cy = (_expr(frames, k, t0) for k in ("zoom", "x", "y"))
        scaled_w = f"{_num(src_w * factor)}*({z})"
        scaled_h = f"{_num(src_h * factor)}*({z})"
        scaled = g.add(src, f"scale=w='2*ceil(max({rw},{scaled_w})/2)':h='2*ceil(max({rh},{scaled_h})/2)':"
                            "eval=frame:flags=bicubic,setsar=1")
        base = g.add([], f"color=c=black:s={rw}x{rh}:r={fps}:d={_num(piece['frames'] / fps + 0.5)}")
        x = f"-{_clip_expr(f'({cx})*{scaled_w}-{rw / 2}', f'w-{rw}')}"
        y = f"-{_clip_expr(f'({cy})*{scaled_h}-{rh / 2}', f'h-{rh}')}"
        label = g.add([base, scaled], f"overlay=x='{x}':y='{y}':eof_action=repeat:shortest=0")
        return label, {"x": str(rx), "y": str(ry)}
    zoom = start["zoom"]
    sw_f, sh_f = src_w * factor * zoom, src_h * factor * zoom
    sw, sh = max(rw, 2 * math.ceil(sw_f / 2)), max(rh, 2 * math.ceil(sh_f / 2))
    if kind == "static":
        cx, cy = start["x"] * sw_f - rw / 2, start["y"] * sh_f - rh / 2
        x, y = min(max(0, int(round(cx / 2)) * 2), sw - rw), min(max(0, int(round(cy / 2)) * 2), sh - rh)
        label = g.add(src, f"scale={sw}:{sh}:flags=bicubic,setsar=1,crop={rw}:{rh}:{x}:{y}")
    else:
        cx, cy = _expr(frames, "x", t0), _expr(frames, "y", t0)
        x = _clip_expr(f"({cx})*{_num(sw_f)}-{rw / 2}", str(sw - rw))
        y = _clip_expr(f"({cy})*{_num(sh_f)}-{rh / 2}", str(sh - rh))
        label = g.add(src, f"scale={sw}:{sh}:flags=bicubic,setsar=1,crop={rw}:{rh}:x='{x}':y='{y}'")
    return label, {"x": str(rx), "y": str(ry)}


def piece_graph(g: _Graph, index: int, piece: dict, src_dims: tuple, canvas: dict, width: int, height: int) -> str:
    """Filter chain for one piece; returns the label of its canvas-size yuv420p stream with exact frame count."""
    fps = canvas["fps"]
    layout, rects = piece["layout"], region_rects(piece["layout"], width, height)
    modes = tl.REGION_MODES[layout]
    needs_bg = "fit" in modes
    blur = canvas["background"]["kind"] == "blur"
    branches = len(modes) + (1 if needs_bg and blur else 0)
    head = g.add(f"{index}:v:0", f"fps={fps},setpts=PTS-STARTPTS,setsar=1")
    if branches > 1:
        outs = [g.label("br") for _ in range(branches)]
        g.lines.append(f"[{head}]split={branches}" + "".join(f"[{o}]" for o in outs))
    else:
        outs = [head]
    canvas_bg = None
    if needs_bg:
        canvas_bg = _bg_blur(g, outs.pop(), width, height) if blur else \
            _bg_color(g, width, height, fps, canvas["background"]["color"], piece["frames"])
    streams = [_region_stream(g, outs[i], modes[i], rects[i], piece["regions"][i], piece, src_dims, fps)
               for i in range(len(modes))]
    if layout == "stack":
        current = g.add([s[0] for s in streams], "vstack=inputs=2")
    elif layout == "fill" and streams[0][1] == {"x": "0", "y": "0"} and not needs_bg:
        current = streams[0][0]
    else:
        current = canvas_bg or _bg_color(g, width, height, fps, "#000000", piece["frames"])
        for label, place in streams:
            current = g.add([current, label], f"overlay=x='{place['x']}':y='{place['y']}':eof_action=repeat:shortest=0")
    return g.add(current, f"format=yuv420p,setsar=1,tpad=stop_mode=clone:stop=90,trim=end_frame={piece['frames']},"
                          "setpts=PTS-STARTPTS", f"pv{index}")


# ------------------------------------------------------------------ audio

def audio_plan(timeline: dict) -> list[dict]:
    """Kept ranges merged where source-contiguous, each with a crossfade length to the next range."""
    merged: list[dict] = []
    for r in tl.program_map(timeline):
        if merged and merged[-1]["source_id"] == r["source_id"] and abs(merged[-1]["src_end"] - r["src_start"]) < 1e-3:
            merged[-1]["src_end"] = r["src_end"]
        else:
            merged.append(dict(r))
    for i, r in enumerate(merged):
        length = r["src_end"] - r["src_start"]
        nxt = merged[i + 1]["src_end"] - merged[i + 1]["src_start"] if i + 1 < len(merged) else None
        r["length"] = length
        r["fade"] = min(CROSSFADE, length / 2, nxt / 2) if nxt is not None else 0.0
    return merged


def audio_command(ffmpeg: str, plan: list[dict], paths: dict, has_audio: dict, output: Path, total: float) -> list[str]:
    cmd = [ffmpeg, "-y", "-v", "error"]
    chains = []
    for i, r in enumerate(plan):
        span = r["length"] + (r["fade"] if i + 1 < len(plan) else 0.0)
        if has_audio[r["source_id"]]:
            cmd += ["-vn", "-ss", _num(r["src_start"]), "-t", _num(span), "-i", str(paths[r["source_id"]])]
            chains.append(f"[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                          f"apad=whole_dur={_num(span)},atrim=duration={_num(span)},asetpts=PTS-STARTPTS[a{i}]")
        else:
            cmd += ["-f", "lavfi", "-t", _num(span), "-i", "anullsrc=r=48000:cl=stereo"]
            chains.append(f"[{i}:a:0]aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
    current = "a0"
    for i in range(1, len(plan)):
        chains.append(f"[{current}][a{i}]acrossfade=d={_num(plan[i - 1]['fade'])}:c1=qsin:c2=qsin[x{i}]")
        current = f"x{i}"
    chains.append(f"[{current}]atrim=duration={_num(total)},asetpts=PTS-STARTPTS[aout]")
    return cmd + ["-filter_complex", ";".join(chains), "-map", "[aout]", "-c:a", "pcm_f32le", "-ar", "48000", str(output)]


def _measure_loudness(ffmpeg: str, wav: Path) -> dict | None:
    af = f"loudnorm=I={LOUDNESS['I']}:TP={LOUDNESS['TP']}:LRA={LOUDNESS['LRA']}:print_format=json"
    result = _run([ffmpeg, "-v", "info", "-i", str(wav), "-af", af, "-f", "null", "-"], timeout=300, what="Loudness measurement")
    blocks = re.findall(r"\{[^{}]*\}", result.stderr or "")
    try:
        data = json.loads(blocks[-1])
        measured = {k: float(data[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}
    except (IndexError, KeyError, ValueError):
        return None
    return measured if math.isfinite(measured["input_i"]) and measured["input_i"] > -69 else None


def _normalise(ffmpeg: str, wav: Path, output: Path, warnings: list[str]) -> Path:
    measured = _measure_loudness(ffmpeg, wav)
    if not measured:
        warnings.append("Audio is silent or too quiet to normalise; loudness was left unchanged.")
        return wav
    af = (f"loudnorm=I={LOUDNESS['I']}:TP={LOUDNESS['TP']}:LRA={LOUDNESS['LRA']}:measured_I={measured['input_i']}:"
          f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
          f"offset={measured['target_offset']}:linear=true,aresample=48000")
    _run([ffmpeg, "-y", "-v", "error", "-i", str(wav), "-af", af, "-c:a", "pcm_f32le", str(output)], timeout=300,
         what="Loudness normalisation")
    return output


# ------------------------------------------------------------------ overlay frames

def write_overlay_sequence(compositor: comp.Compositor, directory: Path, fps: int, total_frames: int) -> tuple[Path | None, dict]:
    """Render distinct scenes to PNG and return an ffconcat list (None when nothing is ever drawn)."""
    plan = compositor.frame_plan(fps, total_frames)
    if all(compositor.is_empty(scene) for *_, scene in plan):
        return None, {"entries": 0, "images": 0}
    directory.mkdir(parents=True, exist_ok=True)
    images: dict[str, Path] = {}
    lines = ["ffconcat version 1.0"]
    for first, count, key, scene in plan:
        if key not in images:
            path = directory / f"scene{len(images):04d}.png"
            compositor.render_scene(scene).save(path, format="PNG", compress_level=1)
            images[key] = path
        lines += [f"file {_concat_quote(str(images[key].resolve()))}", f"duration {count / fps:.6f}"]
    lines.append(f"file {_concat_quote(str(images[plan[-1][2]].resolve()))}")
    sequence = directory / "overlay.ffconcat"
    sequence.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sequence, {"entries": len(plan), "images": len(images)}


# ------------------------------------------------------------------ encode

def _watch_progress(path: Path, total_frames: int, callback: Callable[[float], None], stop: threading.Event) -> None:
    while not stop.wait(0.5):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        frames = re.findall(r"^frame=(\d+)", text, re.MULTILINE)
        if frames:
            callback(min(1.0, int(frames[-1]) / max(1, total_frames)))


def _write_srt(path: Path, cues: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for i, cue in enumerate(cues, 1):
            handle.write(f"{i}\n{_fmt_time(cue['start'])} --> {_fmt_time(cue['end'])}\n{cue['text']}\n\n")


def render(timeline: dict, sources: dict, directory: Path, *, assets: dict | None = None, progress: Callable | None = None,
           preview: bool = False, process_owner=None) -> dict:
    with process_ownership(process_owner):
        return _render(timeline, sources, Path(directory), assets or {}, progress, preview)


def _render(timeline: dict, sources: dict, directory: Path, assets: dict, progress: Callable | None, preview: bool) -> dict:
    started = time.monotonic()
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    canonical = tl.validate(timeline, sources)
    canvas = canonical["canvas"]
    fps = canvas["fps"]
    width, height = output_size(canvas, preview)
    total = tl.duration(canonical)
    total_frames = _frame(total, fps)
    warnings: list[str] = []
    stamps = {"start": started}
    source_ids = list(dict.fromkeys([s["source_id"] for s in canonical["segments"]] + [b["source_id"] for b in canonical["broll"]]))
    paths = {sid: _source_path(sources, sid) for sid in source_ids}
    info = {sid: _probe(path) for sid, path in paths.items()}
    for s in canonical["segments"]:
        if not info[s["source_id"]]["has_audio"]:
            warnings.append(f"Source {s['source_id']!r} has no audio track; its speech ranges are silent.")
    warnings = list(dict.fromkeys(warnings))
    name = "preview" if preview else "short"
    output, srt_path, manifest_path = directory / f"{name}.mp4", directory / f"{name}.srt", directory / f"{name}.manifest.json"
    ffmpeg = _binary("ffmpeg", "CLIPPIE_FFMPEG")
    _notify(progress, 0.02)
    with tempfile.TemporaryDirectory(prefix="clippie-v3-", dir=str(directory)) as temp:
        work = Path(temp)
        # 1. audio
        plan = audio_plan(canonical)
        raw_wav = work / "program.wav"
        _run(audio_command(ffmpeg, plan, paths, {s: i["has_audio"] for s, i in info.items()}, raw_wav, total), timeout=max(120, int(total * 5)),
             what="Audio assembly")
        audio = _normalise(ffmpeg, raw_wav, work / "normalized.wav", warnings) if canonical["audio"]["normalize"] else raw_wav
        stamps["audio"] = time.monotonic()
        _notify(progress, 0.12)
        # 2. overlay frames
        compositor = comp.Compositor(canonical, sources, width, height, assets=assets)
        warnings += compositor.warnings
        sequence, overlay_stats = write_overlay_sequence(compositor, work / "overlay", fps, total_frames)
        _write_srt(srt_path, compositor.srt_cues())
        stamps["overlay"] = time.monotonic()
        _notify(progress, 0.3)
        # 3. picture + encode
        pieces = plan_pieces(canonical, fps)
        cmd = [ffmpeg, "-y", "-v", "error", "-nostats"]
        for piece in pieces:
            piece_len = piece["frames"] / fps + FRAME_MARGIN
            cmd += ["-ss", _num(piece["src_start"]), "-t", _num(piece_len), "-i", str(paths[piece["source_id"]])]
        graph = _Graph()
        labels = [piece_graph(graph, i, p, (info[p["source_id"]]["width"], info[p["source_id"]]["height"]), canvas, width, height)
                  for i, p in enumerate(pieces)]
        current = labels[0] if len(labels) == 1 else graph.add(labels, f"concat=n={len(labels)}:v=1:a=0")
        extra_inputs = 0
        if sequence:
            cmd += ["-safe", "0", "-f", "concat", "-i", str(sequence)]
            current = graph.add([current, f"{len(pieces)}:v:0"], "overlay=x=0:y=0:eof_action=pass:repeatlast=1:format=auto")
            extra_inputs = 1
        if compositor.bar:
            left, top, _, bottom = compositor.bar_rect(0)
            strip = graph.add([], f"color=c=0x{compositor.bar['color'].lstrip('#')}:s={width}x{bottom - top}:r={fps}:d={_num(total + 1)}")
            current = graph.add([current, strip], f"overlay=x='-{width}+{width}*t/{_num(total)}':y={top}:eof_action=pass:format=auto")
        final = graph.add(current, "format=yuv420p")
        audio_index = len(pieces) + extra_inputs
        cmd += ["-i", str(audio)]
        progress_file = work / "progress.txt"
        crf, audio_rate = ("28", "96k") if preview else ("20", "192k")
        cmd += ["-filter_complex", ";".join(graph.lines), "-map", f"[{final}]", "-map", f"{audio_index}:a:0",
                "-t", _num(total_frames / fps), "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", audio_rate, "-movflags", "+faststart",
                "-progress", str(progress_file), str(output)]
        stop = threading.Event()
        watcher = threading.Thread(target=_watch_progress, args=(progress_file, total_frames,
                                   lambda f: _notify(progress, 0.3 + 0.65 * f), stop), daemon=True)
        watcher.start()
        try:
            _run(cmd, timeout=max(600, int(total * 60)), what="Short render")
        finally:
            stop.set()
            watcher.join(timeout=2)
        stamps["encode"] = time.monotonic()
    _notify(progress, 0.96)
    details = _probe(output)
    version = _run([ffmpeg, "-version"], timeout=15, what="FFmpeg version").stdout.splitlines()[0]
    manifest = {
        "renderer": "clippie.render_v3", "ffmpeg": version, "preview": preview, "timeline": canonical,
        "sources": {sid: {"path": str(path), "title": sources[sid].get("title"), **info[sid]} for sid, path in paths.items()},
        "pieces": [{k: p[k] for k in ("kind", "source_id", "layout", "shot_id", "first_frame", "frames", "src_start")} for p in pieces],
        "audio": {"ranges": len(plan), "crossfade_s": CROSSFADE, "normalized": canonical["audio"]["normalize"]},
        "captions": compositor.srt_cues(), "overlay": overlay_stats, "warnings": warnings,
        "metrics": {"duration": details["duration"], "width": details["width"], "height": details["height"], "fps": fps,
                    "frames": total_frames, "render_seconds": round(time.monotonic() - started, 3),
                    "audio_seconds": round(stamps["audio"] - stamps["start"], 3),
                    "overlay_seconds": round(stamps["overlay"] - stamps["audio"], 3),
                    "encode_seconds": round(stamps["encode"] - stamps["overlay"], 3), "video_bytes": output.stat().st_size},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    _notify(progress, 1.0)
    return {"video_path": output, "srt_path": srt_path, "manifest_path": manifest_path, "duration": details["duration"],
            "width": details["width"], "height": details["height"], "warnings": warnings}
