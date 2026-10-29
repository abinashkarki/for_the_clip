"""yt-dlp driver: format selection, bounded pre-flight inspection, and observable, cancellable downloads."""
from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Callable

from .media import Cancelled

QUALITIES = ('standard', 'high', 'small')
DEFAULT_HEIGHT = 1080
HIGH_HEIGHT = 2160
SMALL_HEIGHT = 480
MAX_FPS = 30
INSPECT_TIMEOUT = 25
PARTIAL_SUFFIXES = {'.part', '.ytdl', '.temp', '.json', '.tmp'}


class InspectError(Exception):
    """The link could not be read; the message is safe to show."""


# -- format selection -------------------------------------------------------

def max_height(quality: str = 'standard') -> int:
    try:
        standard = max(144, min(4320, int(os.environ.get('CLIPPIE_DOWNLOAD_HEIGHT', DEFAULT_HEIGHT))))
    except ValueError:
        standard = DEFAULT_HEIGHT
    return min(SMALL_HEIGHT, standard) if quality == 'small' else HIGH_HEIGHT if quality == 'high' else standard


def video_filters(height: int) -> list[str]:
    """Video constraints from most to least preferred: 30 fps or less H.264 first, so no transcode is needed."""
    return [f'[height<={height}]']


def progressive_filters(height: int) -> list[str]:
    return [f'[height<={height}]']


def video_format(quality: str = 'standard') -> str:
    """Select the best available video within the cap, then a combined stream."""
    height = max_height(quality)
    return f'bestvideo[height<={height}]/best[height<={height}]'



def audio_format() -> str:
    return 'bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio'


def single_format(quality: str = 'standard') -> str:
    """One yt-dlp pass that fetches and merges video and audio."""
    height = max_height(quality)
    return f'bestvideo[height<={height}]+bestaudio/best[height<={height}]'



# -- options ----------------------------------------------------------------

def clip_range(range_: dict | None):
    if not range_:
        return {}
    from yt_dlp.utils import download_range_func
    return {'download_ranges': download_range_func(None, [(float(range_['start']), float(range_['end']))]),
            'force_keyframes_at_cuts': True}


def base_options() -> dict:
    return {'noplaylist': True, 'quiet': True, 'no_warnings': True, 'noprogress': True, 'socket_timeout': 30,
            'retries': 5, 'fragment_retries': 5, 'continuedl': True, 'concurrent_fragment_downloads': 4,
            # Within the resolution cap prefer H.264, then at most MAX_FPS: renders are 30 fps, so a 60 fps
            # stream doubles the download and hashing for nothing.
            'format_sort': ['res', 'vcodec:h264', f'fps:{MAX_FPS}']}


def download_options(directory: Path, stem: str, fmt: str, *, merge: bool, range_: dict | None,
                     progress_hook: Callable | None, postprocessor_hook: Callable | None) -> dict:
    # A fixed output name per stem lets yt-dlp find and resume `<stem>.<ext>.part` after a retry or restart.
    opts = {**base_options(), 'format': fmt, 'outtmpl': str(Path(directory) / f'{stem}.%(ext)s'),
            'progress_hooks': [progress_hook] if progress_hook else [],
            'postprocessor_hooks': [postprocessor_hook] if postprocessor_hook else [], **clip_range(range_)}
    if merge:
        opts['merge_output_format'] = 'mp4'
    return opts


def _ytdlp():
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError('Install yt-dlp to ingest web video: python -m pip install yt-dlp') from exc
    return yt_dlp


def resolve_output(info: dict | None, directory: Path, stem: str) -> Path:
    for item in (info or {}).get('requested_downloads') or []:
        found = item.get('filepath') or item.get('_filename')
        if found and Path(found).is_file():
            return Path(found).resolve()
    found = sorted((p for p in Path(directory).glob(f'{stem}*') if p.is_file() and p.suffix not in PARTIAL_SUFFIXES),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if found:
        return found[0].resolve()
    raise RuntimeError('yt-dlp completed without producing a media file.')


def download_file(url: str, directory: Path, *, stem: str, fmt: str, merge: bool = False, range_: dict | None = None,
                  progress_hook: Callable | None = None, postprocessor_hook: Callable | None = None,
                  cancelled: Callable[[], bool] | None = None) -> tuple[Path, dict]:
    """Download one file. Raises `media.Cancelled` when `cancelled()` turns true (checked on every progress tick)."""
    yt_dlp = _ytdlp()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    def hook(status: dict) -> None:
        if cancelled and cancelled():
            raise yt_dlp.utils.DownloadCancelled()
        if progress_hook:
            progress_hook(status)

    opts = download_options(directory, stem, fmt, merge=merge, range_=range_, progress_hook=hook,
                            postprocessor_hook=postprocessor_hook)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        if cancelled and cancelled():
            raise Cancelled() from exc
        if isinstance(exc, yt_dlp.utils.DownloadCancelled):
            raise Cancelled() from exc
        raise
    return resolve_output(info, directory, stem), info or {}


def fetch_single(url: str, directory: Path, *, quality: str = 'standard', range_: dict | None = None, **hooks) -> Path:
    """One-pass download and merge, used when a site has no separate audio stream."""
    try:
        path, _info = download_file(url, directory, stem='source', fmt=single_format(quality), merge=True, range_=range_, **hooks)
    except Cancelled:
        raise
    except Exception as exc:
        raise RuntimeError(f'Could not download source URL: {exc}') from exc
    return path


# -- pre-flight -------------------------------------------------------------

def _size(fmt: dict, duration: float | None) -> int | None:
    value = fmt.get('filesize') or fmt.get('filesize_approx')
    if not value and fmt.get('tbr') and duration:
        value = float(fmt['tbr']) * 1000 / 8 * float(duration)
    return int(value) if value else None


def _select(info: dict, fmt: str) -> list[dict] | None:
    yt_dlp = _ytdlp()
    probe = copy.deepcopy(info)
    for key in ('requested_formats', 'requested_downloads', 'format_id', 'url'):
        probe.pop(key, None)
    try:
        with yt_dlp.YoutubeDL({**base_options(), 'format': fmt, 'simulate': True}) as ydl:
            chosen = ydl.process_ie_result(probe, download=False)
    except Exception:
        return None
    return chosen.get('requested_formats') or [chosen]


def _total(formats: list[dict] | None, duration: float | None) -> int | None:
    sizes = [_size(f, duration) for f in formats or []]
    return sum(sizes) if sizes and all(sizes) else None


def _label(formats: list[dict] | None, fallback: str) -> str:
    video = next((f for f in formats or [] if f.get('vcodec') not in (None, 'none') and f.get('height')), None)
    if not video:
        return fallback
    fps = video.get('fps')
    return f"{int(video['height'])}p" + (f' · {int(round(fps))} fps' if fps else '')


def summarize(info: dict) -> dict:
    """Title, duration and the byte estimate of every download option, from `extract_info(download=False)`."""
    duration = info.get('duration')
    options, sizes = [], {}
    plans = (('standard', video_format('standard'), 'Resolution unknown'),
             ('high', video_format('high'), 'Resolution unknown'),
             ('small', video_format('small'), 'Resolution unknown'))
    audio = _select(info, audio_format())
    audio_bytes = _total(audio, duration)
    for identifier, fmt, fallback in plans:
        chosen = _select(info, fmt)
        video_only = [f for f in chosen or [] if f.get('vcodec') not in (None, 'none')]
        has_audio = any(f.get('acodec') not in (None, 'none') for f in chosen or [])
        total = _total(chosen, duration)
        if total is not None and not has_audio and audio_bytes:
            total += audio_bytes
        sizes[identifier] = total
        video = next((item for item in video_only if item.get('height')), None)
        options.append({'id': identifier, 'label': _label(video_only or chosen, fallback),
                        'height': int(video['height']) if video else None, 'download_bytes': total})
    sizes['audio_first'] = audio_bytes
    options.append({'id': 'audio_first', 'label': 'Audio first', 'download_bytes': audio_bytes, 'strategy': True,
                    'detail': 'Transcription starts as soon as the audio is down, while the video keeps downloading.'})
    return {'title': info.get('title') or info.get('id') or 'Untitled video', 'duration': duration,
            'thumbnail_url': info.get('thumbnail'), 'uploader': info.get('uploader') or info.get('channel'),
            'webpage_url': info.get('webpage_url'), 'id': info.get('id'), 'language': info.get('language'),
            'options': options, 'download_bytes': sizes}


def extract(url: str) -> dict:
    yt_dlp = _ytdlp()
    opts = {**base_options(), 'skip_download': True, 'socket_timeout': 10, 'extractor_retries': 1}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise InspectError(f'Could not read that link: {str(exc)[-300:]}') from exc
    if not info:
        raise InspectError('That link did not return a video.')
    return info


def bounded(function: Callable, timeout: float):
    """Run `function()` on a helper thread; give up (leaving the thread to finish alone) after `timeout` seconds."""
    box: dict = {}

    def run() -> None:
        try:
            box['value'] = function()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box['error'] = exc

    worker = threading.Thread(target=run, daemon=True, name='clippie-inspect')
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise InspectError(f'Reading that link took longer than {int(timeout)} seconds. Check the address and try again.')
    if 'error' in box:
        raise box['error']
    return box['value']


def inspect(url: str, timeout: float | None = None) -> dict:
    return bounded(lambda: summarize(extract(url)), timeout or INSPECT_TIMEOUT)
