"""Download the YuNet face detector into <data dir>/models/ and verify its SHA256.

Usage: python scripts/prepare_vision.py [--data-dir DIR] [--force]
"""
import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clippie.analyzers import faces  # noqa: E402

MIRRORS = [
    faces.MODEL_URL,
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/" + faces.MODEL_NAME,
]


def fetch(url: str) -> bytes:
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, connect=20)) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def prepare(directory: Path, *, force=False, fetcher=fetch, urls=None, filename=faces.MODEL_NAME) -> Path:
    directory = Path(directory)
    target = directory / filename
    if target.is_file() and not force and faces.sha256_file(target) == faces.MODEL_SHA256:
        return target
    errors = []
    for url in urls or MIRRORS:
        try:
            data = fetcher(url)
        except Exception as exc:  # network failure: try the next mirror
            errors.append(f"{url}: {exc}")
            continue
        if hashlib.sha256(data).hexdigest() != faces.MODEL_SHA256:
            errors.append(f"{url}: SHA256 mismatch")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".yunet-", dir=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
        return target
    raise RuntimeError("Could not download a verified YuNet model:\n" + "\n".join(errors))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.data_dir:
        directory = Path(args.data_dir).expanduser().resolve() / 'models'
        print(prepare(directory, force=args.force))
    else:
        target = faces.model_path()
        print(prepare(target.parent, force=args.force, filename=target.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
