"""Runtime locations shared by source runs and the packaged desktop application.

The launcher sets ``CLIPPIE_DATA_DIR`` to its writable Application Support folder.
Source runs retain their existing repository ``data/`` default. Immutable resources
are relative to the installed ``clippie`` package unless the launcher supplies a
separate ``CLIPPIE_RESOURCE_DIR``. Resolving a location never creates it.
"""
from __future__ import annotations

import os
import json
from pathlib import Path


def _override(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else None


def resource_root() -> Path:
    """Directory containing Clippie's assets, templates and built static editor."""
    return _override('CLIPPIE_RESOURCE_DIR') or Path(__file__).resolve().parent


def data_root() -> Path:
    """Writable project/model root; independent of an immutable resource override."""
    return _override('CLIPPIE_DATA_DIR') or Path(__file__).resolve().parent.parent / 'data'


def model_directory(name: str, *, override: str | None = None) -> Path:
    """Model file/directory, optionally selected by a named environment variable."""
    return (_override(override) if override else None) or data_root() / 'models' / name


def app_version() -> str:
    """Read the immutable bundle manifest; source runs use the development version."""
    package = Path(__file__).resolve().parent
    if package.name != 'clippie' or package.parent.name != 'app' or package.parent.parent.name != 'Resources' or package.parent.parent.parent.name != 'Contents':
        return '0.1.0'
    try:
        value = json.loads((package.parent.parent / 'build-manifest.json').read_text()).get('app_version')
        return value if isinstance(value, str) and value else '0.1.0'
    except (OSError, ValueError, AttributeError):
        return '0.1.0'
