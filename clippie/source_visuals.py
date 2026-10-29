"""Bounded local still evidence. No inference, downloads, or source mutation."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from PIL import Image

from . import render
from .source_evidence import _hash

IMPLEMENTATION_VERSION = 1
CONFIG = {"max_width": 320, "max_height": 180, "jpeg_quality": 60, "resample": "lanczos"}


def _checkpoint(owner):
    if owner is not None:
        owner.checkpoint()


def _validate(directory, expected, times):
    try:
        if directory.is_symlink():
            raise ValueError("symlink directory")
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("symlink manifest")
        artifact = json.loads(manifest_path.read_text())
        if artifact["manifest"] != expected or len(artifact["samples"]) != len(times):
            raise ValueError("manifest mismatch")
        for row, at in zip(artifact["samples"], times):
            sample_id = f"s-{_hash([expected['fingerprint'], at])[:24]}"
            filename = sample_id + ".jpg"
            if row.get("id") != sample_id or row.get("at") != at or row.get("source_id") != expected["source_id"] or row.get("path") != str(directory / filename):
                raise ValueError("sample metadata mismatch")
            path = directory / filename
            if path.is_symlink() or not path.is_file():
                raise ValueError("missing or unsafe sample")
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("image hash mismatch")
            with Image.open(path) as image:
                image.load()
                if image.format != "JPEG" or image.size != (row["width"], row["height"]) or not (0 < image.width <= 320 and 0 < image.height <= 180):
                    raise ValueError("image dimensions or format mismatch")
        return artifact
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Corrupt visual evidence cache at {directory}; preserve or remove this cache directory and retry: {exc}") from exc


def prepare_samples(source, cache_root, times, process_owner=None):
    """Extract one to four exact requested source times, with immutable local caching."""
    if not isinstance(source, dict):
        raise ValueError("source must be an object.")
    source_id = source.get("source_id", source.get("id"))
    duration = source.get("duration")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id or id must be non-empty text.")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("source duration must be finite and positive.")
    if not isinstance(times, (list, tuple)) or not 1 <= len(times) <= 4:
        raise ValueError("sample times must contain one through four entries.")
    if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t < duration for t in times):
        raise ValueError("sample times must be finite numbers inside source duration.")
    times = [float(t) for t in times]
    if len(set(times)) != len(times):
        raise ValueError("sample times must be unique.")
    media = render._source_path(source, source_id)
    stat = media.stat()
    identity = {"path": str(media), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                "inode": stat.st_ino, "device": stat.st_dev}
    payload = {"source_id": source_id, "duration": float(duration), "media_identity": identity,
               "implementation_version": IMPLEMENTATION_VERSION, "config": dict(CONFIG), "times": times}
    fingerprint = _hash(payload)
    manifest = dict(payload, fingerprint=fingerprint, schema_version=1, kind="local_sampled_still_evidence",
                    retention="Local sampled images retained until explicitly deleted.",
                    identity_limit="File stat identity; no full media SHA computed.")
    root = Path(cache_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = root / f"visuals-{fingerprint}"
    _checkpoint(process_owner)
    with (root / f"visuals-{fingerprint}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _checkpoint(process_owner)
        if directory.exists() or directory.is_symlink():
            artifact = _validate(directory, manifest, times)
            _checkpoint(process_owner)
            return {"artifact": artifact, "cache_status": "hit", "path": directory / "manifest.json", "cache_artifact_key": fingerprint}
        temporary = Path(tempfile.mkdtemp(prefix=".visuals-", dir=root))
        try:
            samples = []
            with render.process_ownership(process_owner):
                for at in times:
                    _checkpoint(process_owner)
                    sample_id = f"s-{_hash([fingerprint, at])[:24]}"
                    filename = sample_id + ".jpg"
                    raw = temporary / "frame.png"
                    render._run([render._binary("ffmpeg", "CLIPPIE_FFMPEG"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", repr(at), "-i", str(media), "-frames:v", "1", "-vf", "scale=320:180:force_original_aspect_ratio=decrease", str(raw)], timeout=20, what="Visual evidence sampling")
                    _checkpoint(process_owner)
                    with Image.open(raw) as image:
                        image = image.convert("RGB")
                        image.thumbnail((320, 180), Image.Resampling.LANCZOS)
                        image.save(temporary / filename, format="JPEG", quality=60, optimize=False, progressive=False)
                        width, height = image.size
                    raw.unlink()
                    samples.append({"id": sample_id, "source_id": source_id, "at": at,
                                    "width": width, "height": height, "path": str(directory / filename),
                                    "sha256": hashlib.sha256((temporary / filename).read_bytes()).hexdigest()})
            artifact = {"manifest": manifest, "samples": samples}
            with (temporary / "manifest.json").open("w") as handle:
                json.dump(artifact, handle, allow_nan=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            _checkpoint(process_owner)
            os.replace(temporary, directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return {"artifact": artifact, "cache_status": "build", "path": directory / "manifest.json", "cache_artifact_key": fingerprint}


def load_samples(cache_root, fingerprint):
    """Read only a validated cache-key child; the original media need not exist."""
    import re
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Visual evidence fingerprint must be 64 lowercase hexadecimal characters.")
    directory = Path(cache_root).expanduser().resolve() / f"visuals-{fingerprint}"
    try:
        manifest_path = directory / "manifest.json"
        if directory.is_symlink() or manifest_path.is_symlink():
            raise ValueError("unsafe cache symlink")
        manifest = json.loads(manifest_path.read_text())["manifest"]
        payload_keys = ("source_id", "duration", "media_identity", "implementation_version", "config", "times")
        payload = {key: manifest[key] for key in payload_keys}
        if (manifest["fingerprint"] != fingerprint or _hash(payload) != fingerprint
                or not isinstance(manifest["source_id"], str) or not manifest["source_id"].strip()
                or manifest["schema_version"] != 1 or manifest["kind"] != "local_sampled_still_evidence"
                or manifest["implementation_version"] != IMPLEMENTATION_VERSION or manifest["config"] != CONFIG):
            raise ValueError("manifest identity or configuration mismatch")
        times, duration = manifest["times"], manifest["duration"]
        if (isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0
                or not isinstance(times, list) or not 1 <= len(times) <= 4
                or any(isinstance(t, bool) or not isinstance(t, (float, int)) or not math.isfinite(t) or not 0 <= t < duration for t in times)
                or len(set(times)) != len(times)):
            raise ValueError("invalid sample timing")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Corrupt visual evidence cache at {directory}; preserve or remove this cache directory and retry: {exc}") from exc
    artifact = _validate(directory, manifest, times)
    return {"artifact": artifact, "cache_status": "hit", "path": directory / "manifest.json", "cache_artifact_key": fingerprint}
