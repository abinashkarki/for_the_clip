import hashlib
import threading

import httpx
import pytest

from clippie.model_downloads import DownloadCancelled, DownloadError, download_verified


DATA = b'pinned model data'
ARTIFACT = {'url': 'https://models.example/model', 'size': len(DATA),
            'sha256': hashlib.sha256(DATA).hexdigest()}


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure = chunks, failure

    def __iter__(self):
        yield from self.chunks
        if self.failure:
            raise self.failure


def fetch(tmp_path, handler, *, cancel=None, progress=None, artifact=ARTIFACT):
    with httpx.Client(transport=httpx.MockTransport(handler),
                      headers={'Authorization': 'secret', 'X-API-Key': 'secret'},
                      cookies={'credential': 'secret'}) as client:
        return download_verified(artifact, tmp_path / 'model', cancel=cancel or threading.Event(),
                                 progress=progress or (lambda *_: None), client=client)


def response(data=DATA, status=200, headers=None):
    return httpx.Response(status, headers=headers, stream=Chunks([data]))


def test_interruption_then_resume(tmp_path):
    def interrupted(request):
        assert request.headers['accept-encoding'] == 'identity'
        assert 'authorization' not in request.headers
        assert 'x-api-key' not in request.headers
        assert 'cookie' not in request.headers
        return httpx.Response(200, stream=Chunks([DATA[:5]], httpx.ReadError('sensitive remote text')))
    with pytest.raises(DownloadError, match='connection failed') as error:
        fetch(tmp_path, interrupted)
    assert 'sensitive' not in str(error.value)
    assert (tmp_path / 'model.part').read_bytes() == DATA[:5]
    assert not (tmp_path / 'model').exists()

    def resumed(request):
        assert request.headers['range'] == 'bytes=5-'
        return response(DATA[5:], 206, {'Content-Range': f'bytes 5-{len(DATA)-1}/{len(DATA)}'})
    values = []
    assert fetch(tmp_path, resumed, progress=lambda n, total: values.append(n)).read_bytes() == DATA
    assert values == [5, len(DATA)]


def test_ignored_range_restarts(tmp_path):
    (tmp_path / 'model.part').write_bytes(b'wrong')
    assert fetch(tmp_path, lambda _: response()).read_bytes() == DATA


@pytest.mark.parametrize('header', ['garbage', 'bytes 0-16/17', 'bytes 5-15/17', 'bytes 5-16/18'])
def test_malformed_range_preserves_partial(tmp_path, header):
    (tmp_path / 'model.part').write_bytes(DATA[:5])
    with pytest.raises(DownloadError, match='range is invalid'):
        fetch(tmp_path, lambda _: response(DATA[5:], 206, {'Content-Range': header}))
    assert (tmp_path / 'model.part').read_bytes() == DATA[:5]
    assert not (tmp_path / 'model').exists()


def test_wrong_hash_discards_partial(tmp_path):
    with pytest.raises(DownloadError, match='checksum'):
        fetch(tmp_path, lambda _: response(b'x' * len(DATA)))
    assert not (tmp_path / 'model.part').exists()
    assert not (tmp_path / 'model').exists()


@pytest.mark.parametrize('body', [DATA + b'x', DATA[:-1]])
def test_body_size_enforced(tmp_path, body):
    with pytest.raises(DownloadError):
        fetch(tmp_path, lambda _: response(body))
    assert not (tmp_path / 'model').exists()
    if len(body) < len(DATA):
        assert (tmp_path / 'model.part').read_bytes() == body
    else:
        assert not (tmp_path / 'model.part').exists()


def test_cancel_after_last_byte_preserves_complete_partial(tmp_path):
    cancel = threading.Event()
    def progress(n, total):
        if n == total:
            cancel.set()
    with pytest.raises(DownloadCancelled):
        fetch(tmp_path, lambda _: response(), cancel=cancel, progress=progress)
    assert (tmp_path / 'model.part').read_bytes() == DATA
    assert not (tmp_path / 'model').exists()


def test_cancel_before_transfer(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        fetch(tmp_path, lambda _: pytest.fail('network called'), cancel=cancel)


def test_416_complete_partial_verified_and_installed(tmp_path):
    (tmp_path / 'model.part').write_bytes(DATA)
    assert fetch(tmp_path, lambda _: response(b'', 416)).read_bytes() == DATA


def test_416_invalid_partial_retries_once_from_zero(tmp_path):
    (tmp_path / 'model.part').write_bytes(b'bad')
    requests = []
    def handler(request):
        requests.append(request)
        return response(b'', 416) if len(requests) == 1 else response()
    assert fetch(tmp_path, handler).read_bytes() == DATA
    assert len(requests) == 2
    assert 'range' not in requests[1].headers


def test_repeated_416_stops(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return response(b'', 416)
    with pytest.raises(DownloadError, match='range was rejected'):
        fetch(tmp_path, handler)
    assert len(requests) == 2


@pytest.mark.parametrize('location', ['http://models.example/bad', '//other.example/good', '/good'])
def test_https_redirect_policy(tmp_path, location):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return response(b'', 302, {'Location': location})
        return response()
    if location.startswith('http:'):
        with pytest.raises(DownloadError, match='HTTPS'):
            fetch(tmp_path, handler)
        assert len(requests) == 1
    else:
        assert fetch(tmp_path, handler).read_bytes() == DATA
        assert len(requests) == 2


def test_corrupt_final_quarantined_and_valid_final_reused(tmp_path):
    (tmp_path / 'model').write_bytes(b'corrupt')
    result = fetch(tmp_path, lambda _: response())
    assert result.read_bytes() == DATA
    assert (tmp_path / 'model.corrupt').read_bytes() == b'corrupt'
    assert fetch(tmp_path, lambda _: pytest.fail('network called')) == result


def test_disk_full_sanitized(tmp_path, monkeypatch):
    import errno
    from pathlib import Path
    original = Path.open
    def fail(self, mode='r', *args, **kwargs):
        if mode == 'wb':
            raise OSError(errno.ENOSPC, 'sensitive filesystem detail')
        return original(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', fail)
    with pytest.raises(DownloadError, match='disk space') as error:
        fetch(tmp_path, lambda _: response())
    assert 'sensitive' not in str(error.value)


def test_cancel_between_chunks(tmp_path):
    cancel = threading.Event()
    def progress(n, total):
        if n == 5:
            cancel.set()
    with pytest.raises(DownloadCancelled):
        fetch(tmp_path, lambda _: httpx.Response(200, stream=Chunks([DATA[:5], DATA[5:]])),
              cancel=cancel, progress=progress)
    assert (tmp_path / 'model.part').read_bytes() == DATA[:5]
    assert not (tmp_path / 'model').exists()


def test_cancel_during_verification(tmp_path, monkeypatch):
    import clippie.model_downloads as downloads
    cancel = threading.Event()
    real_sha = hashlib.sha256
    class CancellingHash:
        def __init__(self):
            self.hashed = real_sha()
        def update(self, data):
            self.hashed.update(data)
            cancel.set()
        def hexdigest(self):
            return self.hashed.hexdigest()
    monkeypatch.setattr(downloads.hashlib, 'sha256', CancellingHash)
    with pytest.raises(DownloadCancelled):
        fetch(tmp_path, lambda _: response(), cancel=cancel)
    assert (tmp_path / 'model.part').read_bytes() == DATA
    assert not (tmp_path / 'model').exists()
