"""Deterministic local transcript evidence preprocessing, without semantic inference."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

IMPLEMENTATION_VERSION = 2


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be finite numeric text timing.")
    return float(value)


OVERRUN_TOLERANCE = 1.0      # seconds past the recorded duration that are clamped instead of rejected
MAX_SKIPPED_ROWS = 5           # malformed ASR rows dropped silently before the transcript is rejected
MAX_SKIPPED_SHARE = 0.01


def _rows(rows, duration, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} must be a list.")
    result = []
    previous = -1.
    ids = set()
    skipped = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"{label} must contain non-empty text.")
        start, end = _number(row.get("start"), "start"), _number(row.get("end"), "end")
        # ASR timing is slightly noisy: a final word can end a few hundredths past the (rounded) duration.
        if start < duration and duration < end <= duration + OVERRUN_TOLERANCE:
            end = duration
        if start < 0 or end > duration + 1e-6:
            raise ValueError(f"{label} ranges must be ordered and inside source duration.")
        if start < previous or end <= start:      # zero-length or slightly out-of-order ASR rows
            skipped += 1
            if skipped > max(MAX_SKIPPED_ROWS, len(rows) * MAX_SKIPPED_SHARE):
                raise ValueError(f"{label} ranges must be ordered and inside source duration.")
            continue
        previous = start
        identity = row.get("id", index)
        if isinstance(identity, bool) or not isinstance(identity, (str, int)) or identity in ids:
            raise ValueError(f"{label} IDs must be unique strings or integers.")
        ids.add(identity)
        speaker = row.get("speaker")
        if speaker is not None and (not isinstance(speaker, str) or not speaker.strip()):
            raise ValueError("speaker must be non-empty text when supplied.")
        result.append({"id": identity, "text": row["text"].strip(), "start": start, "end": end,
                       "speaker": speaker, "approximate": bool(row.get("approximate", False))})
    return result


def _artifact(evidence):
    return evidence.get("artifact", evidence)


def packets(evidence):
    """Return complete-source bounded packets; their overlap is explicitly labelled."""
    return _artifact(evidence)["packets"]


def context_for(unit_id, evidence, before=1, after=1):
    """Expand exact neighboring evidence independently of any derived summary."""
    if any(type(n) is not int or n < 0 or n > 100 for n in (before, after)):
        raise ValueError("context counts must be integers from 0 through 100.")
    units = _artifact(evidence)["units"]
    index = next((i for i, unit in enumerate(units) if unit["id"] == unit_id), None)
    if index is None:
        raise ValueError("Unknown evidence unit ID.")
    selected = units[max(0, index-before):index+after+1]
    return {"target_unit_id": unit_id, "units": selected, "start": selected[0]["start"],
            "end": selected[-1]["end"], "text": " ".join(unit["text"] for unit in selected)}


def _build(payload, fingerprint, max_chunk_chars):
    words, segments = payload["words"], payload["segments"]
    groups = []
    if words:
        current = []
        for word in words:
            # Never split a punctuated sentence solely to satisfy packet sizing.
            if current and (word["start"] - current[-1]["end"] > 1.5 or
                            (word["speaker"] is not None and current[-1]["speaker"] is not None
                             and word["speaker"] != current[-1]["speaker"])):
                groups.append(current)
                current = []
            current.append(word)
            if re.search(r'[.!?。！？]["\'”’)]*$', word["text"]):
                groups.append(current)
                current = []
        if current:
            groups.append(current)
    else:
        groups = [[segment] for segment in segments]
    transcript_hash = _hash({key: payload[key] for key in ("source_id", "words", "segments", "transcript_version", "alignment", "implementation_version")})
    units = []
    for index, group in enumerate(groups):
        approximate = not words or payload["alignment"] != "word" or any(row["approximate"] for row in group)
        speakers = sorted({row["speaker"] for row in group if row["speaker"] is not None})
        units.append({"id": f"u-{transcript_hash[:20]}-{index:06d}", "source_id": payload["source_id"],
                      "start": group[0]["start"],
                      "end": max(row["end"] for row in group), "text": " ".join(row["text"] for row in group),
                      "word_ids": [row["id"] for row in group] if words else [],
                      "segment_ids": [row["id"] for row in group] if not words else [],
                      "speakers": speakers, "speaker_status": "unknown" if any(row["speaker"] is None for row in group) else "provided_unverified",
                      "alignment": "approximate" if approximate else "word",
                      "warnings": (["Approximate alignment; verify source cut boundaries."] if approximate else []) +
                                  (["Speaker identity unknown."] if any(row["speaker"] is None for row in group) else [])})
    # Fragment oversized units only in transport packets; evidence units stay intact.
    fragments = []
    for unit in units:
        text = unit["text"]
        for offset in range(0, len(text), max_chunk_chars):
            fragments.append({"unit_id": unit["id"], "text": text[offset:offset+max_chunk_chars],
                              "text_start": offset, "text_end": min(len(text), offset+max_chunk_chars),
                              "start": unit["start"], "end": unit["end"],
                              "partial_unit": len(text) > max_chunk_chars})
    chunks, cursor = [], 0
    while cursor < len(fragments):
        selected, used, end = [], 0, cursor
        while end < len(fragments):
            fragment = fragments[end]
            size = len(fragment["text"]) + bool(selected)
            if used + size > max_chunk_chars:
                break
            selected.append(fragment)
            used += size
            end += 1
        chunks.append({"id": f"p-{fingerprint[:20]}-{len(chunks):06d}", "fragments": selected,
                       "text": " ".join(item["text"] for item in selected),
                       "unit_ids": list(dict.fromkeys(item["unit_id"] for item in selected)),
                       "start": min(item["start"] for item in selected),
                       "end": max(item["end"] for item in selected),
                       "overlap_with_previous": bool(chunks and cursor < previous_end)})
        previous_end = end
        cursor = end - 1 if len(selected) > 1 and end < len(fragments) else end
    return {"manifest": {"schema_version": 1, "implementation_version": IMPLEMENTATION_VERSION,
                         "fingerprint": fingerprint, "transcript_hash": transcript_hash,
                         "source_id": payload["source_id"], "transcript_version": payload["transcript_version"],
                         "content_bearing_local_retention_version": 1,
                         "retention": "Local transcript text retained until explicitly deleted; prior cache versions preserved.",
                         "kind": "deterministic_evidence_preprocessing", "semantic_topic_map": "not_generated",
                         "max_chunk_chars": max_chunk_chars,
                         "media_identity": payload.get("media_identity"),
                         "provided_content_hash": payload.get("content_hash"),
                         "identity_limit": "Transcript evidence and optional file stat identity; no full media SHA computed."},
            "transcript": {"words": words, "segments": segments}, "units": units, "packets": chunks}


def _payload(source, max_chunk_chars):
    if not isinstance(source, dict):
        raise ValueError("source must be an object.")
    if type(max_chunk_chars) is not int or not 64 <= max_chunk_chars <= 100000:
        raise ValueError("max_chunk_chars must be an integer from 64 through 100000.")
    duration = _number(source.get("duration"), "duration")
    if duration <= 0:
        raise ValueError("source duration must be positive.")
    words = _rows(source.get("words") or [], duration, "words")
    segments = _rows(source.get("segments") or [], duration, "segments")
    if not words and not segments:
        raise ValueError("Ready transcript words or segments are required.")
    source_id = source.get("source_id", source.get("id"))
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id or id must be non-empty text.")
    payload = {"source_id": source_id, "duration": duration,
               "words": words, "segments": segments, "transcript_version": source.get("transcript_version"),
               "alignment": source.get("alignment", "approximate"),
               "implementation_version": IMPLEMENTATION_VERSION, "max_chunk_chars": max_chunk_chars}
    payload["content_hash"] = source.get("content_hash", source.get("source_hash"))
    source_path = source.get("source_path")
    if source_path is not None:
        media_path = Path(source_path).expanduser().resolve()
        try:
            stat = media_path.stat()
            identity = {"path": str(media_path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "inode": stat.st_ino, "device": stat.st_dev}
        except FileNotFoundError:
            identity = {"path": str(media_path), "missing": True}
        payload["media_identity"] = identity
    return payload


def evidence_key(source, max_chunk_chars=6000):
    """Pure transcript-evidence key; file stat identity is not a full media SHA guarantee."""
    return _hash(_payload(source, max_chunk_chars))


def prepare_evidence(source, cache_root, max_chunk_chars=6000):
    """Cache immutable evidence from existing ready words/segments; return hit/build."""
    payload = _payload(source, max_chunk_chars)
    fingerprint = _hash(payload)
    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"evidence-{fingerprint}.json"
    with (root / f"evidence-{fingerprint}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_file():
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ValueError(f"Corrupt evidence cache artifact: {path}") from exc
            if not isinstance(artifact, dict) or not isinstance(artifact.get("manifest"), dict) or artifact["manifest"].get("fingerprint") != fingerprint:
                raise ValueError(f"Evidence cache fingerprint mismatch: {path}")
            return {"artifact": artifact, "cache_status": "hit", "path": path, "cache_artifact_key": fingerprint}
        artifact = _build(payload, fingerprint, max_chunk_chars)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as handle:
                temporary = handle.name
                json.dump(artifact, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    return {"artifact": artifact, "cache_status": "build", "path": path, "cache_artifact_key": fingerprint}
