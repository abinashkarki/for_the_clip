"""Source intake: network and local lanes, audio-first download, staged progress, cancel and retry.

A URL source is fetched on the network lane (audio first, then video). As soon as the audio lands, transcription is
queued on the local lane, so it runs while the video is still downloading. When the video lands it is merged with
the audio on the network lane and its browser preview is queued on the local lane. The source becomes ready when
both the transcript and the preview exist; whichever branch finishes last runs candidate selection.
A local file runs preview and transcription one after the other on the local lane.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from . import download, media, selection, telemetry
from .intake_progress import (STAGE_VERBS, Stats, Tracker, download_detail, format_bytes, format_clock, format_eta,
                              stage_views, BUSY_STATUSES)
from .media import Cancelled
from .store import now

NETWORK_WORKERS = 2
LOCAL_WORKERS = 1
TICK_SECONDS = 1.0
DEFAULT_RTF = {'mlx': 0.04, 'groq': 0.015}   # transcription seconds per audio second until one is measured
DEFAULT_DOWNLOAD_BPS = 3_500_000
RECOVERY_TEXT = ('The app stopped during processing. Retry to continue; anything already downloaded or transcribed is kept.')


# -- lanes ----------------------------------------------------------------

class Lane:
    """A bounded worker pool that remembers who is running and who is waiting, for queue positions."""

    def __init__(self, name: str, workers: int):
        self.name, self.workers = name, workers
        self.lock = threading.Lock()
        self.entries: list[dict] = []
        self.sequence = itertools.count(1)
        self.pool: ThreadPoolExecutor | None = None

    def executor(self) -> ThreadPoolExecutor:
        with self.lock:
            if self.pool is None:
                self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix=f'clippie-{self.name}')
            return self.pool

    def submit(self, job_id: str, kind: str, function: Callable, *args):
        entry = {'job_id': job_id, 'kind': kind, 'seq': next(self.sequence), 'state': 'queued'}

        def run():
            with self.lock:
                entry['state'] = 'running'
            try:
                return function(*args)
            finally:
                with self.lock:
                    if entry in self.entries:
                        self.entries.remove(entry)

        with self.lock:
            self.entries.append(entry)
        try:
            return self.executor().submit(run)
        except BaseException:
            with self.lock:
                if entry in self.entries:
                    self.entries.remove(entry)
            raise

    def bind(self, job_id: str, kind: str):
        """An object with `.submit(fn)` so `telemetry.submit` can run work on this lane."""
        lane = self
        return SimpleNamespace(submit=lambda function: lane.submit(job_id, kind, function))

    def snapshot(self) -> list[dict]:
        with self.lock:
            return sorted((dict(e) for e in self.entries), key=lambda e: e['seq'])

    def shutdown(self) -> None:
        with self.lock:
            pool, self.pool = self.pool, None
            self.entries.clear()
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)


NETWORK = Lane('network', NETWORK_WORKERS)
LOCAL = Lane('local', LOCAL_WORKERS)


def shutdown_lanes() -> None:
    for tracker in list(Tracker._registry.values()):
        tracker.abort()   # stop yt-dlp and FFmpeg promptly; the jobs stay busy in the store and recover as failed on the next start
    NETWORK.shutdown()
    LOCAL.shutdown()


def queue_info(job_id: str) -> dict | None:
    """Where a waiting job stands: `{lane, ahead, position, message}`; None when it is not waiting behind others."""
    for lane in (NETWORK, LOCAL):
        entries = lane.snapshot()
        mine = next((e for e in entries if e['job_id'] == job_id and e['state'] == 'queued'), None)
        if mine is None:
            continue
        ahead = [e for e in entries if e['seq'] < mine['seq'] and e['job_id'] != job_id]
        if not ahead:
            continue
        videos = len({e['job_id'] for e in ahead})
        running = [e for e in ahead if e['state'] == 'running']
        return {'lane': lane.name, 'ahead': videos, 'position': videos + 1,
                'message': f"Waiting · {videos} video{'s' if videos != 1 else ''} ahead{blocker(running)}"}
    return None


def blocker(running: list[dict]) -> str:
    """' (transcribing, ~8 min left)' for the running job expected to free its worker first."""
    best, best_eta = None, math.inf
    for entry in running:
        tracker = Tracker.peek(entry['job_id'])
        active = next((s for s in (tracker.stages if tracker else []) if s['status'] == 'active'), None)
        if active is None:
            continue
        eta = active.get('eta_seconds')
        if best is None or (eta is not None and eta < best_eta):
            best, best_eta = active, eta if eta is not None else math.inf
    if best is None:
        return ''
    left = format_eta(best.get('eta_seconds'))
    return f" ({STAGE_VERBS[best['id']]}{', ' + left if left else ''})"


# -- the view shared by the app and the studio API ------------------------

def progress_view(job: dict) -> dict:
    tracker = Tracker.peek(job['id'])
    busy = job['status'] in BUSY_STATUSES
    stages = [dict(s) for s in tracker.stages] if tracker and busy else (job.get('stages') or [])
    views = stage_views(stages, tracker)
    waiting = queue_info(job['id']) if busy and not any(v['status'] == 'active' for v in views) else None
    if job['status'] == 'ready':
        overall = 1.0
    else:
        overall = tracker.overall() if tracker and busy else float(job.get('overall') or 0.0)
    active = next((v for v in views if v['status'] == 'active'), None)
    message = None
    if busy:
        message = (f"{active['label']} · {active['detail']}" if active and active.get('detail') else active['label'] if active else
                   waiting['message'] if waiting else 'Waiting to start')
    stalled = [v for v in views if v.get('stalled')]
    return {'stages': views, 'overall': overall, 'queue': waiting, 'message': message,
            'stalled': bool(stalled), 'stall_reason': stalled[0].get('stall_reason') if stalled else None,
            'eta_seconds': active.get('eta_seconds') if active else None}


# -- environment ----------------------------------------------------------

@dataclass
class Env:
    store: object
    keys: dict
    select: Callable          # app.do_select(job_id, request)
    clean: Callable           # app.clean_error(error)

    @property
    def stats(self) -> Stats:
        return Stats(self.store.root)


def is_url(source: str) -> bool:
    return str(source).startswith(('http://', 'https://'))


def stages_for(source: str) -> list[str]:
    return ['download', 'merge', 'preview', 'transcribe', 'index'] if is_url(source) else ['preview', 'transcribe', 'index']


def lane_for(source: str) -> Lane:
    return NETWORK if is_url(source) else LOCAL


def wants_audio_first(request) -> bool:
    return request.transcriber in {'auto', 'mlx', 'groq'} and not request.transcript_path


def short_error(error: BaseException) -> str:
    return (str(error).strip().splitlines() or [type(error).__name__])[-1][:240]


# -- entry point (runs on a lane thread) -----------------------------------

def run(env: Env, job_id: str, request) -> None:
    tracker = Tracker.open(env.store, job_id)
    try:
        if is_url(request.source):
            fetch_url(env, tracker, request)
        else:
            local_pipeline(env, tracker, request)
    except Cancelled:
        pass  # the cancel endpoint, or the failure that aborted this step, already recorded the outcome
    except Exception as error:
        fail_job(env, tracker, error)


def fail_job(env: Env, tracker: Tracker, error: BaseException) -> None:
    if tracker.cancelled.is_set():
        return
    telemetry.failure(error)
    tracker.abort()
    with tracker.lock:
        for stage in tracker.stages:
            if stage['status'] == 'active':
                tracker.fail(stage['id'], short_error(error))
    env.store.update(tracker.job_id, status='failed', error=env.clean(error),
                     progress='Processing stopped. Your completed transcript and previous runs are preserved.')
    tracker.flush(force=True, final=True)


def run_branch(env: Env, job_id: str, request, function: Callable, *args) -> None:
    """A follow-up step queued on a lane: same outcomes as `run`."""
    tracker = Tracker.open(env.store, job_id)
    try:
        tracker.check()
        function(env, tracker, request, *args)
    except Cancelled:
        pass
    except Exception as error:
        fail_job(env, tracker, error)


def queue_branch(env: Env, lane: Lane, job_id: str, request, kind: str, function: Callable, *args) -> None:
    telemetry.submit(lane.bind(job_id, kind), 'ingest', run_branch, env, job_id, request, function, *args,
                     source_id=job_id, step=kind)


# -- download ----------------------------------------------------------------

class DownloadMeter:
    """Turns yt-dlp progress dicts into one download stage across every file of the job."""

    def __init__(self, tracker: Tracker, expected_total: float | None):
        self.tracker, self.expected = tracker, expected_total
        self.files: dict[str, list[float | None]] = {}
        self.first: dict[str, float] = {}
        self.speed: float | None = None
        self.started = time.monotonic()

    def totals(self) -> tuple[float, float | None]:
        done = sum(d for d, _t in self.files.values())
        seen = sum(t or 0 for _d, t in self.files.values())
        total = max(self.expected or 0, seen) or None
        return done, total

    def session_bytes(self) -> float:
        return sum(max(0.0, d - self.first.get(name, 0.0)) for name, (d, _t) in self.files.items())

    def hook(self, event: dict) -> None:
        status, name = event.get('status'), str(event.get('filename') or event.get('tmpfilename') or 'file')
        done = float(event.get('downloaded_bytes') or 0)
        total = event.get('total_bytes') or event.get('total_bytes_estimate')
        self.first.setdefault(name, done)
        if status == 'finished':
            total = event.get('total_bytes') or done or total
            done = float(total or done)
        self.files[name] = [done, float(total) if total else None]
        if event.get('speed'):
            self.speed = float(event['speed'])
        hint = None
        if status == 'error':
            hint = 'The site reported a download error; yt-dlp is retrying.'
        done_all, total_all = self.totals()
        eta = (total_all - done_all) / self.speed if total_all and self.speed and total_all >= done_all else event.get('eta')
        fraction = min(done_all / total_all, 0.999) if total_all else None
        self.tracker.progress('download', fraction, download_detail(done_all, total_all, self.speed, eta),
                              eta=float(eta) if eta is not None else None, hint=hint)


def expected_bytes(meta: dict, quality: str, range_: dict | None) -> float | None:
    total = (meta.get('download_bytes') or {}).get(quality)
    duration = meta.get('duration')
    if total and range_ and duration:
        total = total * min(1.0, (range_['end'] - range_['start']) / duration)
    if not total and meta.get('duration'):
        length = (range_['end'] - range_['start']) if range_ else meta['duration']
        total = length * 600_000  # about 4.8 Mbit/s of video and audio when the site gave no sizes
    return float(total) if total else None


def fetch_url(env: Env, tracker: Tracker, request) -> None:
    store, job_id = env.store, tracker.job_id
    directory = store.directory(job_id)
    range_ = request.range.model_dump() if getattr(request, 'range', None) else None
    quality = getattr(request, 'quality', 'standard') or 'standard'
    tracker.check()
    tracker.start('download', 'Reading video details…', stall_after=90)
    with telemetry.stage('ingest'):
        meta = download.bounded(lambda: download.summarize(download.extract(request.source)), 90)
        if range_ and meta.get('duration'):
            if range_['start'] >= meta['duration']:
                raise ValueError(f"The part starts at {format_clock(range_['start'])}, but the video is only {format_clock(meta['duration'])} long.")
            range_['end'] = min(range_['end'], meta['duration'])
        length = (range_['end'] - range_['start']) if range_ else meta.get('duration')
        store.update(job_id, title=meta['title'], expected_duration=length, remote_thumbnail=meta.get('thumbnail_url'),
                     provenance={'input': request.source, 'id': meta.get('id'), 'webpage_url': meta.get('webpage_url'),
                                 'channel': meta.get('uploader'), 'language': meta.get('language'),
                                 'remote_duration': meta.get('duration')})
        estimated = (meta.get('download_bytes') or {}).get(quality)
        if estimated and range_ and meta.get('duration'):
            estimated *= min(1.0, (range_['end'] - range_['start']) / meta['duration'])
        check_free_space(directory, estimated)
        expected = expected_bytes(meta, quality, range_)
        weigh(env, tracker, length, expected, provider_of(request))
        meter = DownloadMeter(tracker, expected)
        tracker.progress('download', 0.0, 'Starting download…')
        audio = audio_pass(env, tracker, request, directory, meter, quality, range_) if wants_audio_first(request) else None
        if audio is None:
            return single_pass(env, tracker, request, directory, meter, quality, range_)
        video = video_pass(env, tracker, request, directory, meter, quality, range_)
        record_download_speed(env, meter)
        tracker.finish('download', f'{format_bytes(meter.totals()[0])} downloaded')
        source = join_streams(tracker, directory, video, audio)
        adopt_source(env, tracker, source)
    queue_branch(env, LOCAL, job_id, request, 'preview', preview_branch)


def audio_pass(env, tracker, request, directory, meter, quality, range_):
    """Fetch the audio stream alone and queue transcription from it right away. None when the site has no audio stream."""
    try:
        path, _info = download.download_file(request.source, directory, stem='audio', fmt=download.audio_format(), range_=range_,
                                             progress_hook=meter.hook, cancelled=tracker.should_stop)
    except Cancelled:
        raise
    except Exception as error:
        tracker.progress('download', None, 'No separate audio stream; downloading video and audio together…',
                         hint=short_error(error))
        return None
    validate_remote_identity(env, tracker, _info)
    tracker.required = {'transcript', 'preview'}
    store_audio(env, tracker, path)
    queue_branch(env, LOCAL, tracker.job_id, request, 'transcribe', transcribe_branch, path)
    return path


def store_audio(env, tracker, path: Path) -> None:
    env.store.update(tracker.job_id, audio_path=str(path))


def video_pass(env, tracker, request, directory, meter, quality, range_) -> Path:
    path, _info = download.download_file(request.source, directory, stem='video', fmt=download.video_format(quality),
                                         range_=range_, progress_hook=meter.hook, cancelled=tracker.should_stop)
    validate_remote_identity(env, tracker, _info)
    return path


def validate_remote_identity(env, tracker, info: dict) -> None:
    job = env.store.get(tracker.job_id)
    expected = (job.get('provenance') or {}).get('id')
    actual = info.get('id')
    if job.get('source_freed_at'):
        original = job.get('freed_provider_id')
        if not original or not expected or not actual or str(original) != str(expected) or str(original) != str(actual):
            raise ValueError('The restored video identity could not be verified; add it again.')
    if expected and actual and str(expected) != str(actual):
        raise ValueError('The downloaded stream does not match the inspected source; retry the import.')
    expected_duration = (env.store.get(tracker.job_id).get('provenance') or {}).get('remote_duration')
    actual_duration = info.get('duration')
    if expected_duration is not None and actual_duration is not None:
        if abs(float(expected_duration) - float(actual_duration)) > 1.0:
            raise ValueError('The source duration changed during import; retry the import.')


def single_pass(env, tracker, request, directory, meter, quality, range_) -> None:
    """One yt-dlp run fetches and merges everything; preview and transcription then run in order on the local lane."""
    tracker.required = set()

    def merging(event: dict) -> None:
        if event.get('postprocessor') in {'Merger', 'FFmpegMerger'}:
            if event.get('status') == 'started':
                if (tracker.stage('download') or {}).get('status') == 'active':
                    tracker.finish('download', f'{format_bytes(meter.totals()[0])} downloaded')
                tracker.start('merge', 'Joining video and audio…', stall_after=600)
            elif event.get('status') == 'finished':
                tracker.finish('merge', 'Joined without re-encoding')

    path, _info = download.download_file(request.source, directory, stem='source', fmt=download.single_format(quality), merge=True,
                                         range_=range_, progress_hook=meter.hook, postprocessor_hook=merging,
                                         cancelled=tracker.should_stop)
    validate_remote_identity(env, tracker, _info)
    record_download_speed(env, meter)
    if (tracker.stage('download') or {}).get('status') == 'active':
        tracker.finish('download', f'{format_bytes(meter.totals()[0])} downloaded')
    if (tracker.stage('merge') or {}).get('status') == 'pending':
        tracker.skip('merge', 'Video and audio arrived in one file')
    adopt_source(env, tracker, path)
    queue_branch(env, LOCAL, tracker.job_id, request, 'prepare', local_pipeline)


def join_streams(tracker: Tracker, directory: Path, video: Path, audio: Path) -> Path:
    tracker.check()
    duration = media.probe_duration(video)
    audio_duration = media.probe_duration(audio)
    if abs(duration - audio_duration) > 1.0:
        raise ValueError('Downloaded audio and video durations differ; retry the import.')
    if media.has_audio_stream(video):
        tracker.skip('merge', 'Video already includes audio')
        return video
    tracker.start('merge', 'Joining video and audio…', stall_after=300)

    def moved(seconds: float, speed: float | None) -> None:
        tracker.progress('merge', seconds / duration, f'{format_clock(seconds)} of {format_clock(duration)} · no re-encoding')

    target = media.mux(video, audio, directory / 'source.mp4', on_progress=moved, process=tracker.register,
                       cancelled=tracker.should_stop)
    tracker.finish('merge', 'Joined without re-encoding')
    return target


def check_free_space(directory: Path, estimate: float | None) -> None:
    """Reserve room for the download, muxed master and a possible preview."""
    if estimate and math.isfinite(estimate) and estimate > 0:
        needed = int(estimate * 3)
        if shutil.disk_usage(directory).free < needed:
            raise ValueError(f'Not enough free disk space to import this source; about {format_bytes(needed)} is needed for download and processing.')


def master_provenance(source: Path) -> dict:
    before = source.stat()
    digest = hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('The source changed during import; retry with a stable file.')
    return {'master_version': 1, 'master_sha256': digest.hexdigest(), 'master_size_bytes': after.st_size}


def adopt_source(env: Env, tracker: Tracker, source: Path) -> None:
    info = media.probe(source)
    if not info.get('duration') or info['duration'] <= 0:
        raise ValueError('The source has no usable duration.')
    provenance = {**(env.store.get(tracker.job_id).get('provenance') or {}), **master_provenance(source)}
    env.store.update(tracker.job_id, source_path=str(source), duration=info['duration'], source_info=info, provenance=provenance)
    telemetry.attributes(source_seconds=info['duration'])


def record_download_speed(env: Env, meter: DownloadMeter) -> None:
    elapsed = time.monotonic() - meter.started
    if elapsed >= 5 and meter.session_bytes() >= 5_000_000:
        env.stats.record('download_bytes_per_second', meter.session_bytes() / elapsed)


# -- estimates -----------------------------------------------------------------

def provider_of(request) -> str:
    return 'import' if request.transcript_path else media.resolve_provider(request.transcriber)


def expected_transcribe_seconds(stats: Stats, provider: str, duration: float | None) -> float | None:
    if not duration or provider in {'import', 'none'}:
        return None
    return duration * (stats.get(f'transcribe_rtf.{provider}') or DEFAULT_RTF.get(provider, 0.05))


def weigh(env: Env, tracker: Tracker, duration: float | None, expected_download: float | None, provider: str) -> None:
    """Weight stages by expected seconds, so the overall bar moves with the work that actually dominates."""
    stats = env.stats
    weights = {'index': 5.0, 'merge': 5.0}
    if expected_download:
        weights['download'] = expected_download / (stats.get('download_bytes_per_second') or DEFAULT_DOWNLOAD_BPS)
        weights['merge'] += expected_download / 400_000_000
    if duration:
        weights['preview'] = duration / (stats.get('preview_speed') or 10.0) * 0.3  # most sources need no transcode
        weights['transcribe'] = expected_transcribe_seconds(stats, provider, duration) or 1.0
    tracker.set_weights(weights)


# -- local steps -----------------------------------------------------------------

def preview_step(env: Env, tracker: Tracker, source: Path, directory: Path, info: dict) -> Path:
    tracker.check()
    with telemetry.stage('preview'):
        kind, path = media.preview_plan(source, directory)
        if kind == 'reuse':
            tracker.skip('preview', 'Already plays in the browser')
            return path
        if kind == 'cached':
            tracker.skip('preview', 'Preview already made')
            return path
        duration = float(info['duration'])
        tracker.start('preview', 'Preparing the browser preview…', stall_after=120)

        seen = {}

        def moved(seconds: float, speed: float | None) -> None:
            eta = (duration - seconds) / speed if speed else None
            parts = [f'{format_clock(seconds)} of {format_clock(duration)}']
            if speed:
                parts.append(f'{speed:.1f}x')
                seen['speed'] = speed
            if eta is not None:
                parts.append(format_eta(eta))
            tracker.progress('preview', min(seconds / duration, 0.999), ' · '.join(parts), eta=eta)

        path = media.make_preview(source, directory, duration=duration, on_progress=moved, process=tracker.register,
                                  cancelled=tracker.should_stop)
        if seen.get('speed'):
            env.stats.record('preview_speed', seen['speed'])
        tracker.finish('preview', 'Preview ready')
        return path


class TranscribeMeter:
    """Chunk-by-chunk transcription progress with a measured real-time-factor ETA between chunks."""

    def __init__(self, env: Env, tracker: Tracker, provider: str, duration: float, directory: Path):
        self.env, self.tracker, self.provider, self.duration, self.directory = env, tracker, provider, duration, directory
        self.rtf = env.stats.get(f'transcribe_rtf.{provider}') or DEFAULT_RTF.get(provider, 0.05)
        self.covered = 0.0
        self.words = 0
        self.span = (0.0, 0.0)
        self.began = 0.0
        self.measured: list[tuple[float, float]] = []   # (compute seconds, audio seconds) of chunks run now
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def eta(self, inside: float = 0.0) -> float:
        return max(0.0, (self.duration - self.covered - inside) * self.rtf)

    def detail(self, eta: float, extra: str = '') -> str:
        parts = [f'{format_clock(self.covered)} of {format_clock(self.duration)}']
        if self.words:
            parts.append(f'{self.words:,} words')
        parts.append(format_eta(eta))
        return ' · '.join(p for p in parts if p) + extra

    def started(self, start: float, end: float) -> None:
        self.tracker.check()
        self.span, self.began = (start, end), time.monotonic()
        if self.thread is None:
            self.thread = threading.Thread(target=self.tick, daemon=True, name='clippie-transcribe-tick')
            self.thread.start()

    def finished(self, covered: float, total: float, words: list[dict]) -> None:
        elapsed = time.monotonic() - self.began
        length = self.span[1] - self.span[0]
        if elapsed >= 1.0 and length > 0:
            self.measured.append((elapsed, length))
            done = sum(c for c, _a in self.measured) / sum(a for _c, a in self.measured)
            self.rtf = done  # the speed of this machine right now beats the stored average
        self.covered, self.words = covered, len(words)
        write_partial(self.directory, words, covered, total)
        self.tracker.progress('transcribe', covered / total, self.detail(self.eta()), eta=self.eta())

    def tick(self) -> None:
        """Keep the ETA and elapsed text alive during a long chunk; not progress, so a hung chunk still reads as stalled."""
        while not self.stop.wait(TICK_SECONDS):
            length = max(self.span[1] - self.span[0], 1.0)
            inside = min(0.95, (time.monotonic() - self.began) / max(length * self.rtf, 1.0)) * length
            self.tracker.heartbeat('transcribe', self.detail(self.eta(inside)), eta=self.eta(inside),
                                   fraction=(self.covered + inside) / self.duration)

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        compute = sum(c for c, _a in self.measured)
        audio = sum(a for _c, a in self.measured)
        if audio >= 60 and compute > 0:
            self.env.stats.record(f'transcribe_rtf.{self.provider}', compute / audio)


def write_partial(directory: Path, words: list[dict], covered: float, total: float) -> None:
    try:
        (directory / 'transcript.partial.json').write_text(json.dumps({'covered_seconds': covered, 'duration': total, 'words': words}))
    except (OSError, ValueError):
        pass  # the partial transcript is a recovery aid, never a reason to stop


def reusable_transcript(job: dict, directory: Path):
    """A transcript saved by an earlier attempt for this same source file."""
    saved = directory / 'transcript.json'
    if job.get('words') and saved.is_file():
        try:
            return json.loads(saved.read_text())
        except (OSError, ValueError):
            return None
    return None


def transcribe_step(env: Env, tracker: Tracker, request, source: Path, directory: Path, duration: float) -> dict:
    tracker.check()
    job = env.store.get(tracker.job_id)
    previous = reusable_transcript(job, directory)
    if previous:
        tracker.start('transcribe', 'Reusing the saved transcript')
        tracker.finish('transcribe', f"{len(previous['words']):,} words (saved from an earlier attempt)")
        return previous
    provider = provider_of(request)
    imported = provider == 'import'
    tracker.start('transcribe', 'Loading the timestamped transcript…' if imported else 'Preparing audio…',
                  stall_after=None if imported else max(120.0, 5 * media.MLX_CHUNK_SECONDS * (
                      env.stats.get(f'transcribe_rtf.{provider}') or DEFAULT_RTF.get(provider, 0.05))))
    meter = None if imported else TranscribeMeter(env, tracker, provider, duration, directory)
    hooks = None if imported else media.TranscribeHooks(cancelled=tracker.should_stop, process=tracker.register,
                                                        started=meter.started, finished=meter.finished)
    began = time.monotonic()
    try:
        with telemetry.stage('transcription', transcriber=request.transcriber):
            telemetry.attributes(cache='miss', cache_kind='transcript')
            transcription = media.transcribe(source, directory, provider=request.transcriber, language=request.language,
                                             groq_key=env.keys['groq'] or None, transcript_path=request.transcript_path,
                                             hooks=hooks)
            telemetry.attributes(word_count=len(transcription['words']), model_id=transcription.get('model'),
                                 transcriber=transcription['provider'])
    finally:
        if meter:
            meter.close()
    words = transcription['words']
    if not words:
        raise ValueError('No speech words were found. Try a speech-heavy source or import a transcript.')
    tracker.finish('transcribe', f'{len(words):,} words')
    transcription['seconds'] = round(time.monotonic() - began, 2)
    return transcription


def store_transcript(env: Env, tracker: Tracker, transcription: dict, directory: Path, duration: float | None) -> None:
    media_write = json.dumps(transcription, indent=2, ensure_ascii=False, allow_nan=False)
    (directory / 'transcript.json').write_text(media_write)
    words = transcription['words']
    job = env.store.get(tracker.job_id)
    env.store.update(tracker.job_id, words=words, segments=transcription.get('segments') or selection.build_segments(words),
                     transcriber=transcription['provider'], warnings=transcription.get('warnings', []),
                     metrics={**job.get('metrics', {}), 'transcription_seconds': transcription.get('seconds'),
                              'transcription_model': transcription.get('model'), 'source_seconds': duration,
                              'word_count': len(words)})


# -- branches ---------------------------------------------------------------------

def local_pipeline(env: Env, tracker: Tracker, request) -> None:
    """Preview, then transcription, for a file that is already on disk."""
    store, job_id = env.store, tracker.job_id
    directory = store.directory(job_id)
    tracker.check()
    job = store.get(job_id)
    if job.get('source_path') and Path(job['source_path']).is_file():
        source = Path(job['source_path'])
        info = job.get('source_info') or media.probe(source)
    else:
        with telemetry.stage('ingest'):
            source = media.ingest(request.source, directory)
            info = media.probe(source)
            telemetry.attributes(source_seconds=info.get('duration', 0))
        if not info.get('duration') or info['duration'] <= 0:
            raise ValueError('The source has no usable duration.')
        store.update(job_id, source_path=str(source), title=source.stem, duration=info['duration'], source_info=info,
                     provenance={'input': request.source, **master_provenance(source)})
    weigh(env, tracker, info['duration'], None, provider_of(request))
    preview = preview_step(env, tracker, source, directory, info)
    store.update(job_id, preview_path=str(preview))
    if request.transcriber == 'none' or not info.get('has_audio'):
        tracker.skip('transcribe', 'No speech to transcribe')
    else:
        transcription = transcribe_step(env, tracker, request, source, directory, info['duration'])
        store_transcript(env, tracker, transcription, directory, info['duration'])
    finalize(env, tracker, request)


def preview_branch(env: Env, tracker: Tracker, request) -> None:
    store, job_id = env.store, tracker.job_id
    job = store.get(job_id)
    source, info = Path(job['source_path']), job['source_info']
    preview = preview_step(env, tracker, source, store.directory(job_id), info)
    store.update(job_id, preview_path=str(preview))
    if tracker.mark_done('preview'):
        finalize(env, tracker, request)


def transcribe_branch(env: Env, tracker: Tracker, request, audio: Path) -> None:
    store, job_id = env.store, tracker.job_id
    directory = store.directory(job_id)
    duration = media.probe_duration(audio)
    transcription = transcribe_step(env, tracker, request, audio, directory, duration)
    store_transcript(env, tracker, transcription, directory, duration)
    if tracker.mark_done('transcript'):
        finalize(env, tracker, request)


def finalize(env: Env, tracker: Tracker, request) -> None:
    """Both halves exist: validate the transcript against the video, choose candidates, and mark the source ready."""
    store, job_id = env.store, tracker.job_id
    tracker.check()
    job = store.get(job_id)
    words, duration = job.get('words') or [], job['duration']
    if (request.transcriber == 'none' or not (job.get('source_info') or {}).get('has_audio')) and not words:
        store.update(job_id, status='ready', words=[], segments=[], clips=[], transcriber='none', error=None,
                     progress='Visual source ready for supporting footage.',
                     warnings=['Visual-only source: no speech transcript. Use it for supporting shots.'])
        tracker.skip('index', 'Nothing to select without speech')
        tracker.complete()
        return
    # Source boundary validation prevents imported timestamps rendering outside media.
    if any(not math.isfinite(w['start']) or not math.isfinite(w['end']) or w['start'] < 0 or w['end'] > duration + .5
           or w['end'] < w['start'] for w in words):
        raise ValueError('Transcript timestamps fall outside the source video.')
    tracker.start('index', 'Choosing candidate moments…', stall_after=300)
    env.select(job_id, request)
    tracker.check()
    clips = len(store.get(job_id).get('clips') or [])
    tracker.finish('index', f'{clips} candidate{"s" if clips != 1 else ""}')
    store.update(job_id, metrics={**store.get(job_id).get('metrics', {}),
                                  'ingest_transcribe_seconds': round(time.time() - tracker.created, 2)})
    tracker.complete()


# -- cancel, retry, recovery --------------------------------------------------------------

class NotCancellable(Exception):
    pass


def cancel(env: Env, job_id: str) -> dict:
    job = env.store.get(job_id)
    if job['status'] not in BUSY_STATUSES:
        raise NotCancellable('This source is not being processed.')
    tracker = Tracker.open(env.store, job_id)
    tracker.request_cancel()
    tracker.close_unfinished('skipped', 'Cancelled')
    env.store.update(job_id, status='cancelled', error=None,
                     progress='Cancelled. Downloaded data is kept, so Retry continues where this stopped.')
    tracker.flush(force=True, final=True)
    return env.store.get(job_id)


def recover(store) -> None:
    """At startup: whatever was in flight when the app stopped becomes a failed job that Retry can resume."""
    Tracker.reset()
    for job in store.all():
        if job['status'] in BUSY_STATUSES:
            stages = [({**s, 'status': 'failed', 'detail': 'Interrupted when the app stopped', 'eta_seconds': None}
                       if s.get('status') == 'active' else dict(s)) for s in job.get('stages') or []]
            store.update(job['id'], status='failed', error=RECOVERY_TEXT, stages=stages, progress=RECOVERY_TEXT)
