"""Media ingestion, transcription, preview, and clip export helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _binary(name: str, override: str) -> str:
    candidate = os.environ.get(override)
    if candidate:
        return candidate
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} was not found. Install FFmpeg and ensure {name} is on PATH.")
    return found


def _run(args: list[str], *, timeout: int, what: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{what} timed out after {timeout}s.") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{what} failed: {detail[-1800:]}")
    return result


class Cancelled(Exception):
    """Raised inside intake work when the user cancelled it or a sibling step already failed."""


_FFMPEG_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time", "dup_frames",
                "drop_frames", "speed", "progress"}


def _seconds_from_progress(state: dict[str, str]) -> float | None:
    # FFmpeg's out_time_ms is microseconds too; out_time_us is the correctly named twin.
    for key in ("out_time_us", "out_time_ms"):
        try:
            return max(0.0, int(state[key]) / 1_000_000)
        except (KeyError, ValueError):
            continue
    return None


def run_ffmpeg(args: list[str], *, timeout: int, what: str, on_progress: Callable[[float, float | None], None] | None = None,
               process: Callable[[subprocess.Popen], None] | None = None,
               cancelled: Callable[[], bool] | None = None) -> None:
    """Run FFmpeg with `-progress pipe:1`; report (output seconds, speed) and stop promptly when cancelled."""
    command = [_binary("ffmpeg", "CLIPPIE_FFMPEG"), "-y", "-v", "error", "-nostats", "-progress", "pipe:1", *args]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    if process:
        process(proc)
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    tail: deque[str] = deque(maxlen=20)
    state: dict[str, str] = {}
    try:
        for line in proc.stdout or ():
            key, sep, value = line.strip().partition("=")
            if sep and key in _FFMPEG_KEYS:
                state[key] = value
                if key == "progress" and on_progress:
                    seconds = _seconds_from_progress(state)
                    if seconds is not None:
                        try:
                            speed = float(state.get("speed", "").rstrip("x"))
                        except ValueError:
                            speed = None
                        on_progress(seconds, speed)
            elif line.strip():
                tail.append(line.strip())
            if cancelled and cancelled():
                proc.kill()
                break
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()
    if cancelled and cancelled():
        raise Cancelled()
    if timed_out.is_set():
        raise RuntimeError(f"{what} timed out after {timeout}s.")
    if proc.returncode:
        raise RuntimeError(f"{what} failed: {chr(10).join(tail)[-1800:]}")


def probe_duration(path: Path) -> float:
    """Duration of any media file, audio-only included."""
    path = Path(path).expanduser().resolve()
    result = _run([_binary("ffprobe", "CLIPPIE_FFPROBE"), "-v", "error", "-show_entries", "format=duration",
                   "-of", "json", str(path)], timeout=30, what="Media probe")
    try:
        duration = float(json.loads(result.stdout).get("format", {}).get("duration") or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read the duration of {path}.") from exc
    if duration <= 0:
        raise RuntimeError(f"No usable duration in {path}.")
    return duration


def has_audio_stream(path: Path) -> bool:
    result = _run([_binary("ffprobe", "CLIPPIE_FFPROBE"), "-v", "error", "-select_streams", "a", "-show_entries",
                   "stream=codec_type", "-of", "json", str(Path(path))], timeout=30, what="Media probe")
    return bool(json.loads(result.stdout or "{}").get("streams"))


def display_dimensions(video: dict) -> tuple[int, int]:
    """Match FFmpeg autorotation/SAR handling used by the Studio renderer."""
    width, height = int(video["width"]), int(video["height"])
    rotation = 0.0
    for side in video.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = float(side["rotation"])
    rotation = rotation or float((video.get("tags") or {}).get("rotate", 0) or 0)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    match = re.fullmatch(r"(\d+):(\d+)", str(video.get("sample_aspect_ratio", "")))
    if match and int(match.group(2)) and int(match.group(1)):
        sar = int(match.group(1)) / int(match.group(2))
        if abs(sar - 1) > 0.01:
            width = round(width * sar)
    return width, height


def probe(path: Path) -> dict:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Media file does not exist: {path}")
    ffprobe = _binary("ffprobe", "CLIPPIE_FFPROBE")
    result = _run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        timeout=30,
        what="Media probe",
    )
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = float(video.get("duration") or data.get("format", {}).get("duration") or 0)
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read media metadata from {path}.") from exc
    if not duration or not width or not height:
        raise RuntimeError(f"No usable video stream found in {path}.")
    display_width, display_height = display_dimensions(video)
    return {"duration": duration, "width": width, "height": height, "has_audio": audio,
            "display_width": display_width, "display_height": display_height,
            "video_codec": video.get("codec_name"), "pixel_format": video.get("pix_fmt"),
            "audio_codec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None),
            "format_name": data.get("format", {}).get("format_name", "")}


def ingest(source: str, directory: Path) -> Path:
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    source = str(source).strip()
    if source.startswith(("http://", "https://")):
        from . import download
        return download.fetch_single(source, directory)
    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Source file does not exist: {path}")
    return path


def mux(video: Path, audio: Path, target: Path, *, duration: float | None = None, on_progress=None,
        process=None, cancelled=None) -> Path:
    """Join a video-only file and an audio file without re-encoding. Written aside, then renamed."""
    target = Path(target)
    partial = target.with_name(f"{target.stem}.partial{target.suffix}")
    def merge(audio_codec: list[str]) -> None:
        run_ffmpeg(["-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *audio_codec,
                    "-movflags", "+faststart", str(partial)], timeout=3600, what="Merging video and audio",
                   on_progress=on_progress, process=process, cancelled=cancelled)

    try:
        try:
            merge(["-c:a", "copy"])
        except RuntimeError:
            merge(["-c:a", "aac", "-b:a", "128k"])  # an audio codec MP4 cannot hold as-is, such as Opus from WebM
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def preview_plan(source: Path, directory: Path) -> tuple[str, Path]:
    """('cached' | 'reuse' | 'encode', path): whether the browser preview needs an FFmpeg pass."""
    source = Path(source).expanduser().resolve()
    target = Path(directory).expanduser().resolve() / "preview.mp4"
    metadata = target.with_suffix('.source.json')
    try:
        stat = source.stat()
        recorded = json.loads(metadata.read_text())
        if (target.is_file() and target.stat().st_size and recorded == {
                'path': str(source), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}):
            _verify_preview(source, target)
            return "cached", target
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        pass
    details = probe(source)
    if ("mp4" in details["format_name"].split(",") and details["video_codec"] == "h264"
            and details["pixel_format"] == "yuv420p" and details["audio_codec"] in {None, "aac"}
            and details["width"] <= 1920 and details["height"] <= 1080):
        # The app serves media paths directly with byte ranges, so a compatible
        # source is already a browser-ready preview. Avoid copying hundreds of MB.
        return "reuse", source
    return "encode", target


def _verify_preview(source: Path, preview: Path) -> None:
    """Reject an incomplete or materially different local preview before using it for editing."""
    original, result = probe(source), probe(preview)
    if (abs(original['duration'] - result['duration']) > .5
            or (original['has_audio'] and not result['has_audio'])):
        raise RuntimeError('The local preview does not match the source duration or audio. Try preparing it again.')
    source_aspect = original['display_width'] / original['display_height']
    preview_aspect = result['display_width'] / result['display_height']
    if abs(preview_aspect / source_aspect - 1) > .01:
        raise RuntimeError('The local preview does not match the source framing. Try preparing it again.')


def make_preview(source: Path, directory: Path, *, duration: float | None = None, on_progress=None,
                 process=None, cancelled=None) -> Path:
    source = Path(source).expanduser().resolve()
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    kind, target = preview_plan(source, directory)
    if kind != "encode":
        return target
    partial = target.with_name("preview.partial.mp4")
    try:
        before = source.stat()
        run_ffmpeg(["-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                    "-vf", "scale='min(960,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                    "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(partial)],
                   timeout=max(900, int((duration or 0) * 2)), what="Preview encoding",
                   on_progress=on_progress, process=process, cancelled=cancelled)
        _verify_preview(source, partial)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError('The source changed while preparing its preview. Try again.')
        os.replace(partial, target)  # an interrupted encode never leaves a playable-looking preview
        metadata = target.with_suffix('.source.json')
        temp = metadata.with_suffix('.json.tmp')
        temp.write_text(json.dumps({'path': str(source), 'size': after.st_size, 'mtime_ns': after.st_mtime_ns}))
        os.replace(temp, metadata)
    finally:
        partial.unlink(missing_ok=True)
    return target


def _format_timestamp(value: float) -> str:
    ms = max(0, round(float(value) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _parse_timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def _segments_from_words(words: list[dict]) -> list[dict]:
    segments: list[dict] = []
    group: list[dict] = []
    for word in words:
        if group and (word["start"] - group[-1]["end"] > 0.8 or re.search(r"[.!?][\"']?$", group[-1]["text"])):
            segments.append(_segment(group))
            group = []
        group.append(word)
    if group:
        segments.append(_segment(group))
    return segments


def _segment(group: list[dict]) -> dict:
    return {"id": len(group) and group[0]["id"], "text": " ".join(w["text"].strip() for w in group).strip(),
            "start": group[0]["start"], "end": group[-1]["end"],
            "start_word_id": group[0]["id"], "end_word_id": group[-1]["id"]}


def _normalise_words(raw_words: list[dict], *, offset: float = 0) -> list[dict]:
    words = []
    for item in raw_words:
        text = str(item.get("text", item.get("word", ""))).strip()
        if not text or item.get("start") is None or item.get("end") is None:
            continue
        try:
            start, end = float(item["start"]) + offset, float(item["end"]) + offset
        except (ValueError, TypeError):
            continue
        if start < 0 or end <= start:
            continue
        words.append({"id": len(words), "text": text, "start": start, "end": end})
    return words


def _approximate_words(segments: list[dict], offset: float = 0) -> list[dict]:
    """Distribute segment duration over tokens; explicitly approximate, never invented speech."""
    words: list[dict] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        try:
            start, end = float(segment["start"]) + offset, float(segment["end"]) + offset
        except (KeyError, ValueError, TypeError):
            continue
        tokens = text.split()
        if end <= start or not tokens:
            continue
        total = sum(max(1, len(token.strip(".,!?;:").encode("utf-8"))) for token in tokens)
        cursor = start
        for i, token in enumerate(tokens):
            weight = max(1, len(token.strip(".,!?;:").encode("utf-8")))
            word_end = end if i == len(tokens) - 1 else cursor + (end - start) * weight / total
            words.append({"id": len(words), "text": token, "start": cursor, "end": word_end})
            cursor = word_end
    return words


def _load_import(path: Path) -> tuple[list[dict], list[dict], list[str]]:
    warnings: list[str] = []
    suffix = path.suffix.lower()
    raw_words = None
    raw_segments = []
    if suffix == ".srt":
        content = path.read_text(encoding="utf-8-sig")
        for block in re.split(r"\n\s*\n", content.strip()):
            lines = block.splitlines()
            timing = next((line for line in lines if "-->" in line), None)
            if not timing:
                continue
            left, right = [v.strip().split()[0] for v in timing.split("-->", 1)]
            text = " ".join(line.strip() for line in lines[lines.index(timing) + 1:] if line.strip())
            raw_segments.append({"start": _parse_timestamp(left), "end": _parse_timestamp(right), "text": text})
    elif suffix in {".json", ".json5"}:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            raw_segments = data
        elif isinstance(data, dict):
            raw_words = data.get("words")
            raw_segments = data.get("segments", [])
        else:
            raise ValueError("Transcript JSON must be an object or list.")
    else:
        raise ValueError("Transcript import must be .json or .srt.")
    if not raw_words and raw_segments:
        raw_words = [word for segment in raw_segments for word in segment.get("words", [])]
    if raw_words:
        words = _normalise_words(raw_words)
    else:
        words = _approximate_words(raw_segments)
        warnings.append("Imported transcript has segment-only timing; individual word timings are approximate.")
    if not words:
        raise ValueError(f"No usable speech timings found in {path}.")
    segments = _segments_from_words(words)
    return words, segments, warnings


_MLX_FFMPEG_LOCK = threading.Lock()


def _ml_transcribe(source: Path, language: str | None, model: str, *,
                   allow_empty: bool = False) -> tuple[list[dict], list[dict], bool]:
    # Xet's parallel range transfer is unreliable on some client networks; keep
    # the normal signed CDN HTTP path unless the operator configured otherwise.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    try:
        import mlx_whisper
    except ImportError as exc:
        raise RuntimeError("Local transcription requires mlx-whisper on Apple Silicon. Install with: pip install 'clippie[local]' (or choose Groq/import).") from exc
    # MLX Whisper shells out to `ffmpeg`; honor the app override while restoring PATH.
    ffmpeg_override = os.environ.get("CLIPPIE_FFMPEG")
    with _MLX_FFMPEG_LOCK:
        original_path = os.environ.get("PATH", "")
        if ffmpeg_override:
            os.environ["PATH"] = str(Path(ffmpeg_override).expanduser().resolve().parent) + os.pathsep + original_path
        try:
            result = mlx_whisper.transcribe(str(source), path_or_hf_repo=model,
                                            word_timestamps=True, language=language)
        finally:
            if ffmpeg_override:
                os.environ["PATH"] = original_path
    raw_words = result.get("words") or []
    raw_segments = result.get("segments") or []
    approximate = False
    if raw_words:
        words = _normalise_words(raw_words)
    else:
        words = _normalise_words([w for segment in raw_segments for w in segment.get("words", [])])
    if not words:
        words = _approximate_words(raw_segments)
        approximate = bool(words)
    if not words:
        # A successful, explicitly empty Whisper result is valid for one silent
        # chunk (including an AAC padding tail). The complete recording must
        # still contain speech; malformed/nonempty untimed output stays fatal.
        if (allow_empty and isinstance(result.get("segments"), list) and not raw_segments
                and not raw_words and isinstance(result.get("text", ""), str)
                and not result.get("text", "").strip()):
            return [], [], False
        raise RuntimeError("MLX Whisper returned no usable speech timings.")
    return words, _segments_from_words(words), approximate


@dataclass
class TranscribeHooks:
    """Optional observers for chunked transcription; every member may stay None."""
    cancelled: Callable[[], bool] | None = None
    process: Callable[[subprocess.Popen], None] | None = None
    started: Callable[[float, float], None] | None = None          # (chunk start, chunk end) in source seconds
    finished: Callable[[float, float, list[dict]], None] | None = None  # (covered seconds, total, words so far)

    def check(self) -> None:
        if self.cancelled and self.cancelled():
            raise Cancelled()


MLX_CHUNK_SECONDS = 10 * 60
GROQ_CHUNK_SECONDS = 9 * 60
CHUNK_OVERLAP = 2.0


def _chunk_cache_file(cache: tuple[Path, str] | None, start: float) -> Path | None:
    return cache[0] / f"{cache[1]}-{round(start * 1000)}.json" if cache else None


def _chunked_transcribe(source: Path, directory: Path, duration: float, run_chunk, *, chunk_seconds: float,
                        hooks: TranscribeHooks | None = None, cache: tuple[Path, str] | None = None,
                        what: str = "Audio chunk preparation") -> tuple[list[dict], list[dict], bool]:
    """Transcribe `source` in overlapping chunks and stitch the results at a seam inside each overlap.

    `run_chunk(chunk_path, start)` returns `(words offset to source time, approximate)`. Finished chunks are cached
    so a retry after an interruption skips them.
    """
    hooks = hooks or TranscribeHooks()
    ffmpeg = _binary("ffmpeg", "CLIPPIE_FFMPEG")
    overlap = CHUNK_OVERLAP
    words: list[dict] = []
    segments: list[dict] = []
    approximate = False
    start = 0.0
    while start < duration:
        hooks.check()
        end = min(duration, start + chunk_seconds)
        if hooks.started:
            hooks.started(start, end)
        cached = _chunk_cache_file(cache, start)
        parsed = None
        if cached and cached.is_file():
            try:
                saved = json.loads(cached.read_text())
                parsed, approx = saved["words"], bool(saved["approximate"])
            except (OSError, ValueError, KeyError):
                parsed = None
        if parsed is None:
            chunk = directory / f"chunk_{round(start * 1000)}.wav"
            try:
                # Mono 16 kHz PCM keeps each bounded request small and is what Whisper resamples to anyway.
                _run([ffmpeg, "-y", "-v", "error", "-ss", str(start), "-i", str(source), "-t", str(end - start),
                      "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(chunk)], timeout=300, what=what)
                hooks.check()
                parsed, approx = run_chunk(chunk, start)
            finally:
                chunk.unlink(missing_ok=True)
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"words": parsed, "approximate": approx}))
        approximate = approximate or approx
        # Choose a fixed seam halfway through the two-second overlap. The previous result owns speech before it and
        # the current result owns speech after it, so a truncated previous chunk can be repaired.
        seam = start + overlap / 2 if start else 0.0
        if start:
            words = [w for w in words if w["start"] < seam]
            segments = [s for s in segments if s["start"] < seam]
            parsed = [w for w in parsed if w["start"] >= seam]
        words.extend(parsed)
        segments.extend(_segments_from_words(parsed))
        if hooks.finished:
            hooks.finished(end, duration, words)
        if end >= duration:
            break
        start = end - overlap
    words.sort(key=lambda w: (w["start"], w["end"]))
    for index, word in enumerate(words):
        word["id"] = index
    return words, _segments_from_words(words), approximate


def _shift(words: list[dict], offset: float) -> list[dict]:
    return [{**w, "start": w["start"] + offset, "end": w["end"] + offset} for w in words]


def _ml_transcribe_chunked(source: Path, directory: Path, language: str | None, model: str, duration: float, *,
                           hooks: TranscribeHooks | None = None, cache: tuple[Path, str] | None = None):
    def run_chunk(chunk: Path, start: float):
        words, _segments, approximate = _ml_transcribe(chunk, language, model, allow_empty=True)
        return _shift(words, start), approximate

    words, segments, approximate = _chunked_transcribe(source, directory, duration, run_chunk,
                                                       chunk_seconds=MLX_CHUNK_SECONDS, hooks=hooks, cache=cache)
    if not words:
        raise RuntimeError("MLX Whisper returned no usable speech timings.")
    return words, segments, approximate


def _groq_transcribe(source: Path, directory: Path, language: str | None, key: str, *, duration: float | None = None,
                     hooks: TranscribeHooks | None = None, cache: tuple[Path, str] | None = None):
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("Groq transcription requires httpx. Install the project dependencies with: pip install -e .") from exc
    duration = duration or probe(source)["duration"]
    with httpx.Client(timeout=180) as client:
        def run_chunk(chunk: Path, start: float):
            data = {"model": "whisper-large-v3-turbo", "response_format": "verbose_json",
                    "timestamp_granularities[]": ["word", "segment"]}
            if language:
                data["language"] = language
            try:
                with chunk.open("rb") as audio:
                    response = client.post("https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"}, data=data,
                        files={"file": (chunk.name, audio, "audio/wav")})
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                raise RuntimeError(f"Groq transcription failed for audio starting at {start:.1f}s: {exc}") from exc
            got_words = body.get("words") or []
            if got_words:
                return _normalise_words(got_words, offset=start), False
            source_segments = [
                {**s, "start": float(s["start"]) + start, "end": float(s["end"]) + start}
                for s in body.get("segments") or [] if s.get("start") is not None and s.get("end") is not None
            ]
            parsed = _approximate_words(source_segments)
            return parsed, bool(parsed)

        words, segments, approximate = _chunked_transcribe(source, directory, duration, run_chunk,
                                                           chunk_seconds=GROQ_CHUNK_SECONDS, hooks=hooks, cache=cache,
                                                           what="Groq audio chunk preparation")
    if not words:
        raise RuntimeError("Groq returned no usable word or segment timings.")
    return words, segments, approximate


def resolve_provider(provider: str) -> str:
    if provider == "auto":
        return "mlx" if sys.platform == "darwin" and __import__("platform").machine() == "arm64" else "groq"
    return provider


def transcribe(source: Path, directory: Path, provider="auto", language=None, groq_key=None, transcript_path=None,
               *, hooks: TranscribeHooks | None = None) -> dict:
    source = Path(source).expanduser().resolve()
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    model_name: str | None = None
    if transcript_path:
        words, segments, warnings = _load_import(Path(transcript_path).expanduser().resolve())
        actual_provider = "import"
    else:
        selected = resolve_provider(provider)
        stat = source.stat() if source.is_file() else None
        signature = f"{selected}|{language}|{stat.st_size if stat else 0}|{stat.st_mtime_ns if stat else 0}"
        cache = (directory / "transcript-chunks", hashlib.sha1(signature.encode()).hexdigest()[:16])
        if selected == "mlx":
            from .local_models import converted_model_path
            cached = converted_model_path()
            model_name = os.environ.get("CLIPPIE_WHISPER_MODEL") or (
                str(cached) if (cached / 'weights.safetensors').is_file()
                else "mlx-community/whisper-large-v3-turbo"
            )
            if os.getenv('CLIPPIE_DESKTOP') == '1':
                local_model = Path(model_name).expanduser()
                if not all((local_model / name).is_file() for name in ('weights.safetensors', 'config.json')):
                    raise RuntimeError('Local speech model is not prepared. Complete model setup before transcribing; Clippie will not download a different model automatically.')
                model_name = str(local_model.resolve())
            duration = probe_duration(source) if source.is_file() else 0.0
            if duration > MLX_CHUNK_SECONDS * 1.25:
                words, segments, approximate = _ml_transcribe_chunked(source, directory, language, model_name, duration,
                                                                      hooks=hooks, cache=cache)
            else:
                if hooks and hooks.started:
                    hooks.started(0.0, duration)
                words, segments, approximate = _ml_transcribe(source, language, model_name)
                if hooks and hooks.finished:
                    hooks.finished(duration, duration, words)
            actual_provider = "mlx"
        elif selected == "groq":
            key = groq_key or os.environ.get("GROQ_API_KEY")
            if not key:
                raise RuntimeError("Groq transcription needs a Groq API key. Set it in Clippie's settings or choose a transcript import.")
            model_name = "whisper-large-v3-turbo"
            words, segments, approximate = _groq_transcribe(source, directory, language, key,
                                                            duration=probe_duration(source), hooks=hooks, cache=cache)
            actual_provider = "groq"
        else:
            raise ValueError(f"Unknown transcription provider: {provider}")
        shutil.rmtree(cache[0], ignore_errors=True)
        # Some Whisper models only supply segment-level alignment.
        if approximate:
            warnings.append("Word timings are approximate because the transcription provider returned segment-level timings only.")
    return {"words": words, "segments": segments, "provider": actual_provider,
            "model": model_name, "warnings": warnings}
