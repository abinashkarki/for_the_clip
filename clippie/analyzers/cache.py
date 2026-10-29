"""Analyzer result cache keyed by source fingerprint, parameters and version."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..paths import data_root


def data_dir() -> Path:
    return data_root()


def fingerprint(source_path) -> str:
    """Stable identity of a media file: resolved path, size and mtime."""
    path = Path(source_path).expanduser().resolve()
    st = path.stat()
    payload = {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def default_dir(source_path) -> Path:
    return data_dir() / "analysis-v3" / fingerprint(source_path)[:16]


def key(source_path, analyzer: str, version: int, params: dict) -> str:
    payload = {"fp": fingerprint(source_path), "analyzer": analyzer, "version": version, "params": params}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _path(directory, source_path, analyzer, version, digest) -> Path:
    directory = Path(directory) if directory else default_dir(source_path)
    return directory / f"{analyzer}-{version}-{digest[:12]}.json"


def load(directory, source_path, analyzer: str, version: int, params: dict):
    digest = key(source_path, analyzer, version, params)
    path = _path(directory, source_path, analyzer, version, digest)
    try:
        envelope = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(envelope, dict) or envelope.get("key") != digest:
        return None
    return envelope.get("result")


def store(directory, source_path, analyzer: str, version: int, params: dict, result) -> None:
    digest = key(source_path, analyzer, version, params)
    path = _path(directory, source_path, analyzer, version, digest)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".analyzer-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"key": digest, "result": result}, handle, allow_nan=False)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        pass  # a cache that cannot be written never fails an analysis
