"""Size- and digest-pinned, resumable model artifact transfers.

Only verified bytes are promoted to the destination. Interrupted transfers retain
``<destination>.part``; a corrupt existing destination is moved to ``.corrupt``.
Errors deliberately contain no URLs, response bodies, or server-provided text.
"""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import re
import threading
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx


class DownloadCancelled(Exception):
    """The caller cancelled the transfer or verification."""


class DownloadError(RuntimeError):
    """A sanitized artifact transfer or verification failure."""


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise DownloadCancelled("Model download cancelled.")


def _https(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme.lower() == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _verified(path: Path, size: int, digest: str, cancel: threading.Event) -> bool:
    _check_cancel(cancel)
    if path.stat().st_size != size:
        return False
    hashed = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            _check_cancel(cancel)
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            hashed.update(chunk)
    _check_cancel(cancel)
    return hashed.hexdigest() == digest


def _response(client: httpx.Client, url: str, offset: int, cancel: threading.Event) -> httpx.Response:
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    for _ in range(6):
        _check_cancel(cancel)
        if not _https(url):
            raise DownloadError("Model download requires HTTPS.")
        # Construct directly: do not inherit client auth, cookies, or API headers.
        request = httpx.Request("GET", url, headers=headers,
                                extensions={"timeout": httpx.Timeout(30, connect=20).as_dict()})
        response = client.send(request, stream=True, auth=None, follow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location")
        response.close()
        if not location:
            raise DownloadError("Model download redirect is invalid.")
        try:
            url = urljoin(url, location)
        except ValueError:
            raise DownloadError("Model download redirect is invalid.") from None
    raise DownloadError("Model download has too many redirects.")


def download_verified(
    artifact: dict, destination: Path, *, cancel: threading.Event,
    progress: Callable[[int, int], None], client: httpx.Client | None = None,
) -> Path:
    """Fetch an HTTPS artifact, resume safely, and atomically install verified bytes.

    ``progress(received, total)`` reports transfer bytes (including resumed bytes),
    never hashing reads. Network failures and short bodies preserve partial data;
    invalid complete data and oversized bodies are discarded. A 416 response is
    retried once from zero unless the partial file already verifies completely.
    """
    url, size, digest = artifact.get("url"), artifact.get("size"), artifact.get("sha256")
    if (not isinstance(url, str) or not _https(url) or type(size) is not int or size <= 0
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
        raise DownloadError("Invalid pinned model artifact.")
    digest = digest.lower()
    destination = Path(destination)
    partial = Path(str(destination) + ".part")
    owned_client = client is None
    try:
        _check_cancel(cancel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if _verified(destination, size, digest, cancel):
                return destination
            _check_cancel(cancel)
            os.replace(destination, Path(str(destination) + ".corrupt"))
        if partial.exists() and partial.stat().st_size > size:
            partial.unlink()
        if client is None:
            client = httpx.Client(trust_env=False)
        retried_416 = False
        while True:
            _check_cancel(cancel)
            offset = partial.stat().st_size if partial.exists() else 0
            response = _response(client, url, offset, cancel)
            try:
                _check_cancel(cancel)
                if response.status_code == 416:
                    if partial.exists() and _verified(partial, size, digest, cancel):
                        break
                    partial.unlink(missing_ok=True)
                    if retried_416:
                        raise DownloadError("Model download range was rejected.")
                    retried_416 = True
                    continue
                if response.status_code not in (200, 206):
                    raise DownloadError("Model download server rejected the request.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise DownloadError("Model download encoding is invalid.")
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
                    if not match or tuple(map(int, match.groups())) != (offset, size - 1, size):
                        raise DownloadError("Model download range is invalid.")
                else:
                    offset = 0  # Server ignored Range: truncate, never append.
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) != size - offset):
                    raise DownloadError("Model download size header is invalid.")
                received = offset
                with partial.open("ab" if response.status_code == 206 else "wb") as output:
                    progress(received, size)
                    for chunk in response.iter_raw():
                        _check_cancel(cancel)
                        if len(chunk) > size - received:
                            output.close()
                            partial.unlink(missing_ok=True)
                            raise DownloadError("Model download exceeded its pinned size.")
                        output.write(chunk)
                        received += len(chunk)
                        progress(received, size)
                    _check_cancel(cancel)
                    output.flush()
                    os.fsync(output.fileno())
                if received != size:
                    raise DownloadError("Model download ended before its pinned size.")
                if not _verified(partial, size, digest, cancel):
                    partial.unlink(missing_ok=True)
                    raise DownloadError("Model download checksum verification failed.")
                break
            finally:
                response.close()
        _check_cancel(cancel)
        # Also sync a complete partial recovered through HTTP 416.
        with partial.open("rb") as source:
            os.fsync(source.fileno())
        _check_cancel(cancel)
        os.replace(partial, destination)
        return destination
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise DownloadError("Not enough disk space for the model download.") from None
        raise DownloadError("Could not read or write the model download.") from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise DownloadError("Model download connection failed; retry to resume.") from None
    finally:
        if owned_client and client is not None:
            client.close()
