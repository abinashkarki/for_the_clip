"""FFmpeg access for analyzers: raw frames over a pipe and mono audio, always with timeouts."""
from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Iterator

import numpy as np

from .. import media


def probe(path) -> dict:
    return media.probe(Path(path))


def span(info: dict, start, end) -> tuple[float, float]:
    start = max(0.0, float(start or 0))
    end = float(info["duration"] if end is None else min(float(end), info["duration"]))
    if end <= start:
        raise ValueError(f"Empty analysis range {start}..{end}")
    return start, end


def scaled_size(info: dict, width: int) -> tuple[int, int]:
    width = max(16, int(width)) // 2 * 2
    height = max(2, round(info["height"] * width / info["width"] / 2) * 2)
    return width, height


def iter_frames(path, *, start: float, end: float, fps: float, width: int, pix_fmt="gray",
                size: tuple[int, int] | None = None) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (source_time, frame) at `fps` scaled to `width`; frames are HxW or HxWx3."""
    info = probe(path)
    w, h = size or scaled_size(info, width)
    channels = {"gray": 1, "bgr24": 3}[pix_fmt]
    frame_bytes = w * h * channels
    timeout = 120 + 2 * (end - start)
    cmd = [media._binary("ffmpeg", "CLIPPIE_FFMPEG"), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(Path(path).expanduser()),
           "-an", "-sn", "-vf", f"fps={fps:g},scale={w}:{h}:flags=area", "-pix_fmt", pix_fmt,
           "-f", "rawvideo", "pipe:1"]
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            index = 0
            while True:
                buf = proc.stdout.read(frame_bytes)
                while buf is not None and 0 < len(buf) < frame_bytes:
                    more = proc.stdout.read(frame_bytes - len(buf))
                    if not more:
                        break
                    buf += more
                if not buf or len(buf) < frame_bytes:
                    break
                shape = (h, w) if channels == 1 else (h, w, 3)
                yield start + index / fps, np.frombuffer(buf, np.uint8).reshape(shape)
                index += 1
            proc.wait(timeout=10)
            if not timer.is_alive() and proc.returncode not in (0, None):
                raise RuntimeError(f"Frame decode timed out after {timeout:.0f}s.")
            if proc.returncode:
                err.seek(0)
                raise RuntimeError(f"Frame decode failed: {err.read().decode(errors='replace')[-800:]}")
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()


def audio(path, *, start: float, end: float, rate: int = 16000) -> np.ndarray | None:
    """Mono float32 samples in [-1, 1], or None when the file has no audio stream."""
    if not probe(path)["has_audio"]:
        return None
    cmd = [media._binary("ffmpeg", "CLIPPIE_FFMPEG"), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(Path(path).expanduser()),
           "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120 + 2 * (end - start), check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Audio decode timed out.") from exc
    if result.returncode:
        raise RuntimeError(f"Audio decode failed: {result.stderr.decode(errors='replace')[-800:]}")
    usable = len(result.stdout) // 2 * 2
    return np.frombuffer(result.stdout[:usable], np.int16).astype(np.float32) / 32768.0
