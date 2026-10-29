"""Renderer for validated, source-based Clippie productions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable


_PROCESS_OWNER = ContextVar('render_process_owner', default=None)


@contextmanager
def process_ownership(owner):
    token = _PROCESS_OWNER.set(owner)
    try:
        yield
    finally:
        _PROCESS_OWNER.reset(token)


def _binary(name: str, env_name: str) -> str:
    override = os.environ.get(env_name)
    if override:
        return override
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} was not found. Install FFmpeg and ensure {name} is on PATH.")
    return found


def _run(args: list[str], *, timeout: int, what: str) -> subprocess.CompletedProcess[str]:
    try:
        owner = _PROCESS_OWNER.get()
        if owner is None:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        else:
            owner.checkpoint()
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                owner.register_process(process)
                stdout, stderr = process.communicate(timeout=timeout)
                owner.checkpoint()
                result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                for pipe in (getattr(process, 'stdout', None), getattr(process, 'stderr', None)):
                    if pipe is not None:
                        pipe.close()

    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{what} timed out after {timeout}s.") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{what} failed: {detail[-2200:]}")
    return result


def _notify(progress: Callable | None, fraction: float) -> None:
    if progress:
        progress(max(0.0, min(1.0, float(fraction))))


def _validate(timeline: dict, sources: dict) -> dict:
    try:
        from .planner import validate_timeline
    except ImportError as exc:
        raise RuntimeError("Production rendering needs clippie.planner.validate_timeline.") from exc
    return validate_timeline(timeline, sources)


def _source_path(source: dict, source_id: str) -> Path:
    raw = source.get("source_path")
    if not raw:
        raise ValueError(f"Source {source_id!r} has no source_path.")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source {source_id!r} media does not exist: {path}")
    return path


def _probe(path: Path) -> dict:
    ffprobe = _binary("ffprobe", "CLIPPIE_FFPROBE")
    result = _run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                  timeout=30, what="Source probe")
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video")
        duration = float(video.get("duration") or data.get("format", {}).get("duration"))
    except (ValueError, TypeError, StopIteration, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read usable video metadata from {path}.") from exc
    if duration <= 0:
        raise RuntimeError(f"Source has invalid video duration: {path}")
    return {"duration": duration, "width": int(video["width"]), "height": int(video["height"]),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "video_codec": video.get("codec_name"), "pixel_format": video.get("pix_fmt"),
            "audio_codec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)}


def _fmt_time(seconds: float) -> str:
    ms = max(0, round(float(seconds) * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    whole, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{whole:02},{ms:03}"


def _as_words(raw: list[dict], start: float, end: float, at: float) -> list[dict]:
    result = []
    for item in raw or []:
        try:
            word_start, word_end = float(item["start"]), float(item["end"])
            text = str(item.get("text", item.get("word", ""))).strip()
        except (KeyError, ValueError, TypeError):
            continue
        if not text or word_end <= start or word_start >= end or word_end <= word_start:
            continue
        result.append({"text": text, "start": at + max(start, word_start) - start,
                       "end": at + min(end, word_end) - start})
    return result


def _phrases(words: list[dict]) -> list[list[dict]]:
    phrases: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        boundary = bool(current and (
            word.get("clip_index") != current[-1].get("clip_index")
            or
            word["start"] - current[-1]["end"] > 0.65
            or re.search(r"[.!?][\"']?$", current[-1]["text"])
            or len(current) >= 4
        ))
        if boundary:
            phrases.append(current)
            current = []
        current.append(word)
    if current:
        phrases.append(current)
    return phrases


def _caption_data(timeline: dict, sources: dict) -> tuple[list[dict], list[dict]]:
    words: list[dict] = []
    for clip_index, clip in enumerate(timeline["clips"]):
        sid = clip["source_id"]
        source_words = _as_words(sources[sid].get("words", []), float(clip["start"]), float(clip["end"]), float(clip["at"]))
        for word in source_words:
            word["clip_index"] = clip_index
        words.extend(source_words)
    words.sort(key=lambda w: (w["start"], w["end"]))
    cues = []
    events = []
    for phrase in _phrases(words):
        text = " ".join(w["text"] for w in phrase)
        cues.append({"start": phrase[0]["start"], "end": phrase[-1]["end"], "text": text})
        for i, word in enumerate(phrase):
            next_start = phrase[i + 1]["start"] if i + 1 < len(phrase) else word["end"]
            # Keep the phrase visible between words, but clear it during long pauses.
            stop = word["end"] if next_start - word["end"] > 0.65 else max(word["end"], next_start)
            stop = max(word["start"] + 0.04, stop)
            events.append({"start": word["start"], "end": stop, "phrase": phrase, "active": i})
    return cues, events


def _font(size: int, bold: bool = True):
    from PIL import ImageFont

    candidates = (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica Neue Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Helvetica Neue.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _wrap_text(text: str, measure: Callable[[str], float], max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in str(text).split():
        candidate = f"{current} {token}".strip()
        if current and measure(candidate) > max_width:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _render_overlay_frame(path: Path, width: int, height: int, title: str | None,
                          caption: dict | None, caption_style: str) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Burned captions and titles require Pillow. Install Pillow and retry rendering.") from exc

    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    scale = width / 1280
    portrait = height > width
    text_scale = width / 1080 if portrait else scale
    if title:
        title_font_size = max(1, round(48 * text_scale)) if portrait else max(18, round(29 * scale))
        title_font = _font(title_font_size)
        title = title.strip()
        max_title_width = width * 0.84
        title_lines = _wrap_text(title, lambda value: draw.textlength(value, font=title_font), max_title_width)
        minimum_title_size = max(1, round(16 * text_scale)) if portrait else 16
        font_step = max(1, round(2 * text_scale)) if portrait else 2
        while (len(title_lines) > 3 or any(draw.textlength(line, font=title_font) > max_title_width for line in title_lines)) and title_font_size > minimum_title_size:
            title_font_size = max(minimum_title_size, title_font_size - font_step)
            title_font = _font(title_font_size)
            title_lines = _wrap_text(title, lambda value: draw.textlength(value, font=title_font), max_title_width)
        line_metrics = [draw.textbbox((0, 0), line, font=title_font) for line in title_lines]
        line_height = max((b[3] - b[1] for b in line_metrics), default=title_font_size)
        gap = max(1 if portrait else 2, round(4 * scale))
        box_w = max((draw.textlength(line, font=title_font) for line in title_lines), default=0)
        box_h = len(title_lines) * line_height + (len(title_lines) - 1) * gap
        x, y = (width - box_w) / 2, height * 0.075
        pad_x, pad_y = round(18 * scale), round(11 * scale)
        draw.rounded_rectangle((x - pad_x, y - pad_y, x + box_w + pad_x, y + box_h + pad_y),
                               radius=round(8 * scale), fill=(12, 14, 12, 205),
                               outline=(220, 243, 149, 220), width=max(1, round(scale * 2)))
        for index, (line, bounds) in enumerate(zip(title_lines, line_metrics)):
            line_w = draw.textlength(line, font=title_font)
            draw.text((x + (box_w - line_w) / 2, y + index * (line_height + gap) - bounds[1]),
                      line, font=title_font, fill=(250, 249, 241, 255))
    if caption:
        phrase = caption["phrase"]
        font_size = max(1, round(54 * text_scale)) if portrait else max(18, round(36 * scale))
        font = _font(font_size)
        active = -1 if caption_style == "phrase" else int(caption["active"])
        space_w = draw.textlength(" ", font=font)
        max_caption_width = width * 0.84
        while True:
            widths = [draw.textlength(w["text"], font=font) for w in phrase]
            lines: list[list[tuple[int, dict, float]]] = []
            line: list[tuple[int, dict, float]] = []
            line_width = 0.0
            for index, (word, word_width) in enumerate(zip(phrase, widths)):
                needed = word_width + (space_w if line else 0)
                if line and line_width + needed > max_caption_width:
                    lines.append(line)
                    line, line_width = [], 0.0
                    needed = word_width
                line.append((index, word, word_width))
                line_width += needed
            if line:
                lines.append(line)
            too_wide_word = max(widths, default=0) > max_caption_width
            minimum_caption_size = max(1, round(14 * text_scale)) if portrait else 14
            if not too_wide_word or font_size <= minimum_caption_size:
                break
            font_size = max(minimum_caption_size, font_size - (max(1, round(2 * text_scale)) if portrait else 2))
            font = _font(font_size)
            space_w = draw.textlength(" ", font=font)

        line_h = max(round(43 * scale), font_size + round(9 * scale))
        gap = max(1, round(3 * scale))
        box_h = len(lines) * line_h + (len(lines) - 1) * gap
        max_line_w = max((sum(item[2] for item in row) + space_w * (len(row) - 1) for row in lines), default=0)
        pad_x, pad_y = round(20 * scale), round(11 * scale)
        bottom = height - round(0.085 * height)
        top = bottom - box_h
        if caption_style == "phrase":
            # Steady complete phrase: no word-dependent fill, position or backdrop.
            draw.rounded_rectangle(((width - max_line_w) / 2 - pad_x, top - pad_y,
                                    (width + max_line_w) / 2 + pad_x, bottom + pad_y),
                                   radius=max(1, round(6 * scale)), fill=(15, 20, 28, 230),
                                   outline=(205, 213, 222, 150), width=max(1, round(scale)))
        elif caption_style == "editorial":
            # Editorial style uses a full-width lower-third and lime active type.
            draw.rectangle((width * 0.04, top - pad_y, width * 0.96, bottom + pad_y), fill=(8, 10, 8, 224))
        else:
            draw.rounded_rectangle(((width - max_line_w) / 2 - pad_x, top - pad_y,
                                    (width + max_line_w) / 2 + pad_x, bottom + pad_y),
                                   radius=round(10 * scale), fill=(8, 10, 8, 207))
        for row_index, row in enumerate(lines):
            row_w = sum(item[2] for item in row) + space_w * (len(row) - 1)
            x = (width - row_w) / 2
            row_top = top + row_index * (line_h + gap)
            for index, word, word_w in row:
                text = word["text"]
                if index == active and caption_style != "editorial":
                    draw.rounded_rectangle((x - 6 * scale, row_top + 2 * scale,
                                            x + word_w + 6 * scale, row_top + line_h - 2 * scale),
                                           radius=round(5 * scale), fill=(216, 246, 111, 255))
                    fill = (17, 23, 13, 255)
                else:
                    fill = ((255, 255, 255, 255) if caption_style == "phrase" else
                            (216, 246, 111, 255) if index == active else (255, 255, 250, 255))
                bbox = draw.textbbox((0, 0), text, font=font)
                draw.text((x, row_top + (line_h - (bbox[3] - bbox[1])) / 2 - bbox[1]),
                          text, font=font, fill=fill)
                x += word_w + space_w
    image.save(path, format="PNG", optimize=True)


def _concat_quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _overlay_input(directory: Path, timeline: dict, cues: list[dict], word_events: list[dict],
                   duration: float, width: int, height: int) -> tuple[Path | None, list[dict]]:
    titles = timeline.get("titles", [])
    if not titles and not word_events:
        return None, []
    points = {0.0, duration}
    for title in titles:
        start, end = float(title["at"]), float(title["at"]) + float(title["duration"])
        points.update((max(0.0, start), min(duration, end)))
    for event in word_events:
        points.update((max(0.0, event["start"]), min(duration, event["end"])))
    times = sorted(p for p in points if 0 <= p <= duration)
    if len(times) < 2:
        return None, []
    asset_dir = directory / "caption-frames"
    asset_dir.mkdir(parents=True, exist_ok=True)
    assets: dict[str, Path] = {}
    sequence: list[tuple[Path, float]] = []
    frame_manifest: list[dict] = []
    style = str(timeline.get("caption_style", "clean_focus"))
    for left, right in zip(times, times[1:]):
        if right - left < 0.001:
            continue
        mid = (left + right) / 2
        active_title = next((t for t in titles if float(t["at"]) <= mid < float(t["at"]) + float(t["duration"])), None)
        active_event = next((e for e in word_events if e["start"] <= mid < e["end"]), None) if timeline.get("captions", True) else None
        title_text = str(active_title["text"]) if active_title else None
        state = {
            "title": title_text,
            "caption": ({"phrase": active_event["phrase"], "active": -1 if style == "phrase" else active_event["active"]} if active_event else None),
        }
        state_key = hashlib.sha1(json.dumps({
            "title": title_text,
            "caption": ([w["text"] for w in active_event["phrase"]], None if style == "phrase" else active_event["active"]) if active_event else None,
            "size": [width, height], "style": style,
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if state_key not in assets:
            frame = asset_dir / f"{state_key}.png"
            _render_overlay_frame(frame, width, height, title_text, state["caption"], style)
            assets[state_key] = frame
        sequence.append((assets[state_key], right - left))
        frame_manifest.append({"start": left, "end": right, "title": title_text,
                               "caption": " ".join(w["text"] for w in active_event["phrase"]) if active_event else None,
                               "active_word": active_event["phrase"][active_event["active"]]["text"] if active_event and style != "phrase" else None})
    # concat demuxer requires a trailing file entry to preserve the preceding duration.
    if sequence:
        sequence.append((sequence[-1][0], 0.04))
    concat_path = directory / "caption-overlays.ffconcat"
    lines = ["ffconcat version 1.0"]
    for frame, interval in sequence:
        lines.append(f"file {_concat_quote(str(frame.resolve()))}")
        lines.append(f"duration {interval:.6f}")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_path, frame_manifest


def _framing_filter(framing: dict | None, width: int, height: int, *, alpha_pad=False) -> str:
    framing = framing or {}
    mode = framing.get("mode", "fit")
    zoom = float(framing.get("zoom", 1))
    x, y = float(framing.get("x", 0.5)), float(framing.get("y", 0.5))
    if mode not in {"fit", "fill"}:
        raise ValueError(f"Unsupported framing mode: {mode}")
    if not math.isfinite(zoom) or not 1 <= zoom <= 1.5:
        raise ValueError("Framing zoom must be finite and between 1 and 1.5.")
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError("Framing x/y must be between 0 and 1.")
    pad_color = "black@0" if alpha_pad else "black"
    factor = "min" if mode == "fit" else "max"
    scale_factor = f"{factor}({width}/iw\\,{height}/ih)*{zoom}"
    scale_w = f"trunc(iw*({scale_factor})/2)*2"
    scale_h = f"trunc(ih*({scale_factor})/2)*2"
    # Pad only the fit case; max() also handles zoomed fit frames that now crop.
    return (f"scale=w='{scale_w}':h='{scale_h}':eval=frame,setsar=1,"
            f"pad=w='max(iw,{width})':h='max(ih,{height})':x='(ow-iw)*{x:.6f}':y='(oh-ih)*{y:.6f}':color={pad_color},"
            f"crop={width}:{height}:x='(iw-{width})*{x:.6f}':y='(ih-{height})*{y:.6f}',setsar=1")


def _blurred_canvas_filter(input_label, output_label, framing, width, height, fps):
    """One source/time feeds both branches; alpha padding preserves real black pixels."""
    prefix = output_label + "canvas"
    ratio = 160 / max(width, height)
    small_w, small_h = max(2, round(width * ratio / 2) * 2), max(2, round(height * ratio / 2) * 2)
    return (f"[{input_label}]split=2[{prefix}f][{prefix}b];"
            f"[{prefix}b]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
            f"crop={small_w}:{small_h},boxblur=8:2,eq=brightness=-0.12:contrast=0.8,"
            f"scale={width}:{height},setsar=1[{prefix}bg];"
            f"[{prefix}f]format=rgba,{_framing_filter(framing, width, height, alpha_pad=True)}[{prefix}fg];"
            f"[{prefix}bg][{prefix}fg]overlay=0:0:format=auto:shortest=1,"
            f"fps={fps},format=yuv420p[{output_label}]")


def render_production(timeline: dict, sources: dict, directory: Path, progress=None, process_owner=None) -> dict:
    with process_ownership(process_owner):
        return _render_production(timeline, sources, directory, progress)


def _render_production(timeline: dict, sources: dict, directory: Path, progress=None) -> dict:
    """Render the primary speech edit, picture-only overlays, titles and word captions."""
    from PIL import Image  # fail early with the dependency error before long media work

    started = time.monotonic()
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    canonical = _validate(timeline, sources)
    clips = canonical["clips"]
    minimum_clips = 1 if canonical["schema_version"] == 2 else 2
    if len(clips) < minimum_clips:
        raise ValueError("A production needs at least one primary clip." if minimum_clips == 1
                         else "A production needs at least two primary clips.")
    width, height = int(canonical.get("width", 1280)), int(canonical.get("height", 720))
    fps = int(canonical.get("fps", 30))
    duration = float(canonical.get("duration", sum(float(c["end"]) - float(c["start"]) for c in clips)))
    if duration <= 0:
        raise ValueError("Validated production duration must be positive.")
    warnings: list[str] = []
    if canonical.get("broll"):
        warnings.append("B-roll is picture-only; semantic/visual correspondence has not been independently verified.")

    # `at` is computed by the planner. Recompute only as a compatibility fallback.
    cursor = 0.0
    for clip in clips:
        if "at" not in clip:
            clip["at"] = cursor
        cursor += float(clip["end"]) - float(clip["start"])
    source_ids = list(dict.fromkeys([c["source_id"] for c in clips] + [b["source_id"] for b in canonical.get("broll", [])]))
    missing = [sid for sid in source_ids if sid not in sources]
    if missing:
        raise ValueError(f"Timeline references unavailable source IDs: {', '.join(map(str, missing))}")
    source_paths = {sid: _source_path(sources[sid], sid) for sid in source_ids}
    source_info = {sid: _probe(path) for sid, path in source_paths.items()}
    for clip in clips:
        if not source_info[clip["source_id"]]["has_audio"]:
            raise ValueError(f"Primary source {clip['source_id']!r} has no audio track for original speech.")

    srt_cues, word_events = _caption_data(canonical, sources)
    output = directory / "production.mp4"
    srt_path = directory / "production.srt"
    manifest_path = directory / "production-manifest.json"
    with srt_path.open("w", encoding="utf-8") as handle:
        for i, cue in enumerate(srt_cues, 1):
            if cue["end"] <= cue["start"]:
                continue
            handle.write(f"{i}\n{_fmt_time(cue['start'])} --> {_fmt_time(cue['end'])}\n{cue['text']}\n\n")

    ffmpeg = _binary("ffmpeg", "CLIPPIE_FFMPEG")
    with tempfile.TemporaryDirectory(prefix="clippie-render-", dir=str(directory)) as temp:
        work = Path(temp)
        concat_path, overlay_manifest = _overlay_input(work, canonical, srt_cues, word_events,
                                                       duration, width, height)
        _notify(progress, 0.08)
        cmd = [ffmpeg, "-y", "-v", "error"]
        # Give every timeline interval its own bounded input. A shared full
        # source input makes FFmpeg decode from the beginning (or queue frames
        # through a long source) for late and out-of-order excerpts. Input-side
        # seeking plus -t limits work to each selected window; filter trims below
        # are local to that window and keep timestamps stable.
        clip_input_indices: list[int] = []
        for clip in clips:
            sid = clip["source_id"]
            start, end = float(clip["start"]), float(clip["end"])
            clip_input_indices.append(len(clip_input_indices))
            cmd.extend(["-ss", f"{start:.6f}", "-t", f"{end - start:.6f}",
                        "-i", str(source_paths[sid])])
        broll_input_indices: list[int] = []
        for bi, broll in enumerate(canonical.get("broll", [])):
            sid = broll["source_id"]
            start, end = float(broll["start"]), float(broll["end"])
            broll_input_indices.append(len(clip_input_indices) + bi)
            cmd.extend(["-ss", f"{start:.6f}", "-t", f"{end - start:.6f}",
                        "-i", str(source_paths[sid])])
        overlay_index = None
        if concat_path:
            overlay_index = len(clip_input_indices) + len(broll_input_indices)
            cmd.extend(["-safe", "0", "-f", "concat", "-i", str(concat_path)])

        filters: list[str] = []
        video_labels, audio_labels = [], []
        for index, clip in enumerate(clips):
            input_no = clip_input_indices[index]
            start, end = float(clip["start"]), float(clip["end"])
            clip_duration = end - start
            if canonical.get('canvas_style') == 'blurred' and clip.get('framing', {}).get('mode', 'fit') == 'fit':
                filters.append(f"[{input_no}:v:0]trim=start=0:end={clip_duration:.6f},setpts=PTS-STARTPTS[cv{index}]")
                filters.append(_blurred_canvas_filter(f'cv{index}', f'v{index}', clip.get('framing'), width, height, fps))
            else:
                filters.append(f"[{input_no}:v:0]trim=start=0:end={clip_duration:.6f},setpts=PTS-STARTPTS,"
                               f"{_framing_filter(clip.get('framing'), width, height)},fps={fps},format=yuv420p[v{index}]")
            video_labels.append(f"[v{index}]")
            fade = min(0.008, clip_duration / 8)
            audio_filter = (f"[{input_no}:a:0]atrim=start=0:end={clip_duration:.6f},asetpts=PTS-STARTPTS,"
                            "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                            f"afade=t=in:d={fade:.6f},afade=t=out:st={max(0.0, clip_duration-fade):.6f}:d={fade:.6f}[a{index}]")
            filters.append(audio_filter)
            audio_labels.append(f"[a{index}]")
        filters.append(f"{''.join(video_labels)}concat=n={len(clips)}:v=1:a=0[basev]")
        filters.append(f"{''.join(audio_labels)}concat=n={len(clips)}:v=0:a=1[concat_a]")
        current_video = "basev"
        for bi, broll in enumerate(canonical.get("broll", [])):
            input_no = broll_input_indices[bi]
            bstart, bend, at = float(broll["start"]), float(broll["end"]), float(broll["at"])
            b_duration = bend - bstart
            framing = _framing_filter(broll.get("framing"), width, height)
            if canonical.get('canvas_style') == 'blurred' and broll.get('framing', {}).get('mode', 'fit') == 'fit':
                filters.append(f"[{input_no}:v:0]trim=start=0:end={b_duration:.6f},setpts=PTS-STARTPTS+{at:.6f}/TB[cb{bi}]")
                filters.append(_blurred_canvas_filter(f'cb{bi}', f'bv{bi}', broll.get('framing'), width, height, fps))
            else:
                filters.append(f"[{input_no}:v:0]trim=start=0:end={b_duration:.6f},setpts=PTS-STARTPTS+{at:.6f}/TB,"
                               f"{framing},fps={fps},format=yuv420p[bv{bi}]")
            next_video = f"brollv{bi}"
            filters.append(f"[{current_video}][bv{bi}]overlay=x=0:y=0:eof_action=pass:shortest=0:repeatlast=0:"
                           f"enable='between(t,{at:.6f},{at+b_duration:.6f})'[{next_video}]")
            current_video = next_video
        if overlay_index is not None:
            filters.append(f"[{current_video}][{overlay_index}:v:0]overlay=x=0:y=0:eof_action=pass:shortest=0:"
                           "repeatlast=1:format=auto[finalv]")
            current_video = "finalv"
        normalize = bool(canonical.get("audio", {}).get("normalize", True))
        if normalize:
            filters.append("[concat_a]loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
        else:
            filters.append("[concat_a]anull[aout]")
        cmd.extend(["-filter_complex", ";".join(filters), "-map", f"[{current_video}]", "-map", "[aout]",
                    "-t", f"{duration:.6f}", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(output)])
        _notify(progress, 0.22)
        _run(cmd, timeout=max(600, int(duration * 60)), what="Production render")
    _notify(progress, 0.95)

    details = _probe(output)
    ffmpeg_version = _run([ffmpeg, "-version"], timeout=15, what="FFmpeg version").stdout.splitlines()[0]
    source_records = {}
    for sid, path in source_paths.items():
        stat = path.stat()
        source_records[sid] = {"path": str(path), "title": sources[sid].get("title"),
                               "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                               "metadata": source_info[sid]}
    metrics = {"duration": details["duration"], "width": details["width"], "height": details["height"],
               "fps": fps, "primary_clips": len(clips), "broll_overlays": len(canonical.get("broll", [])),
               "caption_words": len(word_events), "caption_phrases": len(srt_cues),
               "render_seconds": round(time.monotonic() - started, 3), "video_bytes": output.stat().st_size}
    manifest = {"renderer": "clippie.render.v1", "ffmpeg": ffmpeg_version, "timeline": canonical,
                "sources": source_records, "warnings": warnings, "metrics": metrics,
                "captions": srt_cues, "overlay_schedule": overlay_manifest,
                "normalization": "single full-program loudnorm pass" if normalize else "disabled",
                "video_codec": "libx264", "pixel_format": "yuv420p", "audio_codec": "aac"}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    _notify(progress, 1.0)
    return {"video_path": output, "srt_path": srt_path, "manifest_path": manifest_path,
            "warnings": warnings, "metrics": metrics}
