"""Streaming multipart intake, filename hygiene and cached FFmpeg thumbnails for the Studio API."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import uuid

import python_multipart
from python_multipart.multipart import parse_options_header

THUMBNAIL_TIMEOUT = 30
MAX_FIELD_BYTES = 4096


class UploadError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def safe_filename(name, default='upload'):
    """Base name only, ASCII-safe, no leading dots, bounded length; keeps a short extension."""
    base = os.path.basename(str(name or '').replace('\\', '/'))
    base = re.sub(r'[^A-Za-z0-9._ -]', '_', base).strip(' .')
    stem, dot, extension = base.rpartition('.')
    if not dot:
        stem, extension = base, ''
    stem = stem.strip(' .') or default
    extension = re.sub(r'[^A-Za-z0-9]', '', extension)[:8]
    return stem[:100] + ('.' + extension if extension else '')


class _Collector:
    """Receives parser callbacks: text fields in memory (bounded), one file straight to disk."""

    def __init__(self, directory, limit, accepts, names):
        self.directory, self.limit, self.accepts, self.names = directory, limit, accepts, names
        self.fields, self.file = {}, None
        self.headers, self._name, self._value = {}, b'', b''
        self.part = None
        self.stream = None

    def on_header_field(self, data, start, end):
        self._name += data[start:end]

    def on_header_value(self, data, start, end):
        self._value += data[start:end]

    def on_header_end(self):
        self.headers[self._name.decode('latin-1').lower()] = self._value.decode('utf-8', 'replace')
        self._name, self._value = b'', b''

    def on_headers_finished(self):
        disposition = self.headers.get('content-disposition', '')
        _, options = parse_options_header(disposition.encode())
        name = options.get(b'name', b'').decode('utf-8', 'replace')
        filename = options.get(b'filename')
        if name not in self.names:
            raise UploadError(422, f'Unexpected form field "{name[:40]}".')
        if name in self.fields or (name == 'file' and self.file):
            raise UploadError(422, f'Send "{name}" only once.')
        self.part = {'name': name, 'size': 0, 'text': bytearray()}
        if filename is not None:
            self._begin_file(name, filename.decode('utf-8', 'replace'))
        elif name == 'file':
            raise UploadError(422, 'The "file" field must be a file upload.')

    def _begin_file(self, name, filename):
        if name != 'file':
            raise UploadError(422, f'"{name}" must be a text field.')
        content_type = self.headers.get('content-type', '').split(';')[0].strip().lower()
        problem = self.accepts(content_type)
        if problem:
            raise UploadError(422, problem)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / safe_filename(filename)
        self.stream = path.open('xb')
        self.file = {'path': path, 'filename': filename, 'content_type': content_type, 'size': 0}

    def on_part_data(self, data, start, end):
        chunk = data[start:end]
        if self.stream is not None:
            self.file['size'] += len(chunk)
            if self.file['size'] > self.limit:
                raise UploadError(413, f'The file is larger than the {self.limit // (1024 * 1024)} MB limit.')
            self.stream.write(chunk)
        else:
            self.part['text'] += chunk
            if len(self.part['text']) > MAX_FIELD_BYTES:
                raise UploadError(422, f'Field "{self.part["name"]}" is too long.')

    def on_part_end(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        elif self.part is not None:
            self.fields[self.part['name']] = self.part['text'].decode('utf-8', 'replace')
        self.part, self.headers = None, {}

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


async def receive_multipart(request, directory, limit, accepts, names=('file', 'title')):
    """Stream a multipart body: returns (text fields, file info or None). Never buffers the file.

    `accepts(content_type)` returns None or a message. Raises UploadError; the caller removes
    `directory` on failure.
    """
    _, options = parse_options_header(request.headers.get('content-type', '').encode())
    boundary = options.get(b'boundary')
    if not request.headers.get('content-type', '').lower().startswith('multipart/form-data') or not boundary:
        raise UploadError(422, 'Send the upload as multipart/form-data.')
    declared = request.headers.get('content-length', '')
    if declared.isdigit() and int(declared) > limit + 65536:
        raise UploadError(413, f'The file is larger than the {limit // (1024 * 1024)} MB limit.')
    collector = _Collector(Path(directory), limit, accepts, set(names))
    parser = python_multipart.MultipartParser(boundary, {
        'on_header_field': collector.on_header_field, 'on_header_value': collector.on_header_value,
        'on_header_end': collector.on_header_end, 'on_headers_finished': collector.on_headers_finished,
        'on_part_data': collector.on_part_data, 'on_part_end': collector.on_part_end})
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
    except UploadError:
        raise
    except Exception as error:
        raise UploadError(422, 'The upload could not be read as multipart form data.') from error
    finally:
        collector.close()
    return collector.fields, collector.file


def ffmpeg_binary():
    return os.environ.get('CLIPPIE_FFMPEG') or 'ffmpeg'


def thumbnail(media_path, at, cache_dir, label):
    """Cached 480 px JPEG of one frame, or None when FFmpeg cannot produce it."""
    media_path = Path(media_path)
    try:
        stat = media_path.stat()
    except OSError:
        return None
    seconds = max(0.0, float(at))
    key = hashlib.sha256(f'{media_path}|{stat.st_mtime_ns}|{stat.st_size}|{seconds:.3f}'.encode()).hexdigest()[:24]
    cache_dir = Path(cache_dir)
    target = cache_dir / f'{label}-{key}.jpg'
    if target.is_file():
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    scratch = cache_dir / f'.{uuid.uuid4().hex}.jpg'
    command = [ffmpeg_binary(), '-v', 'error', '-y', '-ss', f'{seconds:.3f}', '-i', str(media_path),
               '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '4', str(scratch)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=THUMBNAIL_TIMEOUT, check=False)
        if done.returncode == 0 and scratch.is_file() and scratch.stat().st_size:
            os.replace(scratch, target)
            return target
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        scratch.unlink(missing_ok=True)
    return None
