"""Live state of one source job: stages, throttled persistence, stall detection, cancel flags, measured speeds."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

from .media import Cancelled
from .store import now

BUSY_STATUSES = {'queued', 'ingesting', 'transcribing', 'selecting', 'exporting'}
STAGE_LABELS = {'download': 'Download', 'merge': 'Merge', 'preview': 'Browser preview',
                'transcribe': 'Transcription', 'index': 'Finding moments'}
STAGE_VERBS = {'download': 'downloading', 'merge': 'merging', 'preview': 'preparing the preview',
               'transcribe': 'transcribing', 'index': 'finding moments'}
STAGE_ORDER = list(STAGE_LABELS)
DEFAULT_WEIGHTS = {'download': 0.35, 'merge': 0.03, 'preview': 0.12, 'transcribe': 0.45, 'index': 0.05}
WRITE_INTERVAL = 0.5     # seconds between job writes; transitions always write
STALL_SECONDS = 60
STATUS_OF = {'download': 'ingesting', 'merge': 'ingesting', 'preview': 'ingesting',
             'transcribe': 'transcribing', 'index': 'selecting'}
STALL_REASONS = {'download': 'No data has arrived from the video site. The connection may have dropped; Cancel and Retry resumes the download where it stopped.',
                 'merge': 'FFmpeg has not reported progress while joining video and audio.',
                 'preview': 'FFmpeg has not reported progress while preparing the preview.',
                 'transcribe': 'Whisper has not finished a section of audio in the time it should take.',
                 'index': 'Choosing moments is taking longer than expected.'}

clock = time.time  # tests replace this to move time without sleeping


def finite(value):
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


# -- text ---------------------------------------------------------------

def _unit(value: float) -> tuple[float, str]:
    if value >= 1e9:
        return 1e9, 'GB'
    if value >= 1e6:
        return 1e6, 'MB'
    return 1e3, 'KB'


def format_bytes(value: float) -> str:
    divisor, unit = _unit(value)
    return f'{value / divisor:.2f} {unit}' if unit == 'GB' else f'{value / divisor:.1f} {unit}' if unit == 'MB' else f'{value / divisor:.0f} {unit}'


def format_speed(value: float) -> str:
    divisor, unit = _unit(value)
    return f'{value / divisor:.1f} {unit}/s' if unit != 'KB' else f'{value / divisor:.0f} KB/s'


def format_eta(seconds: float | None) -> str:
    """'~6 min left'. Empty when unknown."""
    if seconds is None or seconds < 0 or seconds != seconds:
        return ''
    if seconds < 45:
        return f'~{max(5, int(round(seconds / 5.0) * 5))} s left'
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f'~{max(1, minutes)} min left'
    hours, rest = divmod(minutes, 60)
    return f'~{hours} h {rest:02d} min left' if rest else f'~{hours} h left'


def download_detail(done: float, total: float | None, speed: float | None, eta: float | None) -> str:
    if total and total >= done:
        divisor, unit = _unit(total)
        places = 2 if unit == 'GB' else 1 if unit == 'MB' else 0
        parts = [f'{done / divisor:.{places}f} of {total / divisor:.{places}f} {unit}']
    else:
        parts = [f'{format_bytes(done)} downloaded']
    if speed:
        parts.append(format_speed(speed))
    if eta is not None:
        parts.append(format_eta(eta))
    return ' · '.join(p for p in parts if p)


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f'{hours}:{minutes:02d}:{secs:02d}' if hours else f'{minutes}:{secs:02d}'


# -- measured speeds ----------------------------------------------------

_STATS_LOCK = threading.Lock()


class Stats:
    """`<data dir>/intake-stats.json`: smoothed real-time factors and download speed, used for estimates."""

    def __init__(self, root: Path):
        self.path = Path(root) / 'intake-stats.json'

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str, default: float | None = None) -> float | None:
        value = self.read().get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default

    def record(self, key: str, value: float, weight: float = 0.5) -> None:
        if not value or value <= 0 or value != value or value in (float('inf'),):
            return
        with _STATS_LOCK:
            data = self.read()
            old = data.get(key)
            data[key] = round(value if not isinstance(old, (int, float)) or old <= 0 else old * (1 - weight) + value * weight, 6)
            data['updated_at'] = now()
            temp = self.path.with_suffix('.tmp')
            try:
                temp.write_text(json.dumps(data, indent=2))
                os.replace(temp, self.path)
            except OSError:
                pass  # estimates are a convenience; never fail intake over them


# -- live job state -----------------------------------------------------

class Tracker:
    """Single writer of a job's `stages`; also the job's cancel flag and the FFmpeg processes it owns."""
    _registry: dict[str, 'Tracker'] = {}
    _guard = threading.Lock()

    @classmethod
    def open(cls, store, job_id: str) -> 'Tracker':
        with cls._guard:
            tracker = cls._registry.get(job_id)
            if tracker is None or tracker.store is not store:
                job = store.get(job_id)
                tracker = cls._registry[job_id] = cls(store, job_id, job.get('stages') or [])
            return tracker

    @classmethod
    def peek(cls, job_id: str) -> 'Tracker | None':
        return cls._registry.get(job_id)

    @classmethod
    def forget(cls, job_id: str) -> None:
        with cls._guard:
            cls._registry.pop(job_id, None)

    @classmethod
    def reset(cls) -> None:
        with cls._guard:
            cls._registry.clear()

    def __init__(self, store, job_id: str, stages: list[dict]):
        self.store, self.job_id = store, job_id
        self.stages = [dict(s) for s in stages]
        self.lock = threading.RLock()
        self.weights = dict(DEFAULT_WEIGHTS)
        self.required: set[str] = set()
        self.completed: set[str] = set()
        self.cancelled = threading.Event()
        self.aborted = threading.Event()
        self.processes: set = set()
        self.written = 0.0
        self.dirty = False
        self.high_water = 0.0
        self.stall_after: dict[str, float] = {}
        self.created = time.time()
        self.finished = False   # set by complete(): only then may the overall fraction reach 1
        self.stopped = False    # set by close_unfinished(): the overall fraction freezes where the job stopped

    # planning
    def plan(self, stage_ids, *, required=(), weights=None) -> None:
        with self.lock:
            stamp = now()
            self.stages = [{'id': i, 'label': STAGE_LABELS[i], 'status': 'pending', 'fraction': None, 'detail': None,
                            'eta_seconds': None, 'started_at': None, 'updated_at': stamp, 'progress_at': None,
                            'hint': None} for i in STAGE_ORDER if i in stage_ids]
            self.required, self.completed, self.high_water = set(required), set(), 0.0
            self.finished = self.stopped = False
            self.created = time.time()
            self.cancelled.clear()
            self.aborted.clear()
            self.stall_after = {}
            if weights:
                self.set_weights(weights)
            self.flush(force=True)

    def set_weights(self, weights: dict[str, float]) -> None:
        with self.lock:
            self.weights = {**DEFAULT_WEIGHTS, **{k: max(float(v), 0.001) for k, v in weights.items()}}

    def stage(self, stage_id: str) -> dict | None:
        return next((s for s in self.stages if s['id'] == stage_id), None)

    # transitions (always written)
    def start(self, stage_id: str, detail: str | None = None, *, stall_after: float | None = None) -> None:
        with self.lock:
            stage = self.stage(stage_id)
            if stage is None:
                return
            stamp = clock()
            stage.update(status='active', started_at=stage.get('started_at') or now(), updated_at=now(), progress_at=stamp,
                         detail=detail, hint=None)
            if stall_after:
                self.stall_after[stage_id] = stall_after
            self.flush(force=True)

    def finish(self, stage_id: str, detail: str | None = None) -> None:
        self._end(stage_id, 'done', detail, fraction=1.0)

    def skip(self, stage_id: str, detail: str | None = None) -> None:
        self._end(stage_id, 'skipped', detail, fraction=1.0)

    def fail(self, stage_id: str, detail: str | None = None) -> None:
        self._end(stage_id, 'failed', detail)

    def _end(self, stage_id: str, status: str, detail: str | None, fraction: float | None = None) -> None:
        with self.lock:
            stage = self.stage(stage_id)
            if stage is None:
                return
            stage.update(status=status, detail=detail if detail is not None else stage.get('detail'), updated_at=now(),
                         eta_seconds=None, hint=None, progress_at=clock(),
                         fraction=fraction if fraction is not None else stage.get('fraction'))
            self.flush(force=True)

    def close_unfinished(self, status_active: str = 'failed', detail: str | None = None) -> None:
        """After a stop: active stages end as `status_active`, pending ones are skipped."""
        with self.lock:
            self.overall()
            self.stopped = True
            for stage in self.stages:
                if stage['status'] == 'active':
                    stage.update(status=status_active, detail=detail or stage.get('detail'), eta_seconds=None, updated_at=now())
                elif stage['status'] == 'pending':
                    stage.update(status='skipped', fraction=None, updated_at=now())
            self.flush(force=True)

    # progress (throttled)
    def progress(self, stage_id: str, fraction: float | None = None, detail: str | None = None,
                 eta: float | None = None, hint: str | None = None) -> None:
        with self.lock:
            stage = self.stage(stage_id)
            if stage is None or stage['status'] != 'active':
                return
            if fraction is not None and math.isfinite(fraction):
                stage['fraction'] = round(min(max(float(fraction), 0.0), 1.0), 4)
            stage.update(detail=detail if detail is not None else stage.get('detail'), eta_seconds=finite(eta),
                         hint=hint, updated_at=now(), progress_at=clock())
            self.flush()

    def heartbeat(self, stage_id: str, detail: str | None = None, eta: float | None = None,
                  fraction: float | None = None) -> None:
        """Refresh text and estimates without claiming real progress, so a hung step still goes stale."""
        with self.lock:
            stage = self.stage(stage_id)
            if stage is None or stage['status'] != 'active':
                return
            if fraction is not None and math.isfinite(fraction):
                stage['fraction'] = round(min(max(float(fraction), stage.get('fraction') or 0.0), 0.99), 4)
            stage.update(detail=detail if detail is not None else stage.get('detail'), eta_seconds=finite(eta), updated_at=now())
            self.flush()

    # persistence
    def overall(self) -> float:
        with self.lock:
            if self.finished:
                return 1.0
            if self.stopped:
                return round(self.high_water, 4)
            total = sum(self.weights[s['id']] for s in self.stages) or 1.0
            done = 0.0
            for stage in self.stages:
                if stage['status'] in {'done', 'skipped'}:
                    value = 1.0
                elif stage['status'] == 'active':
                    value = stage.get('fraction') or 0.0
                else:
                    value = 0.0
                done += self.weights[stage['id']] * value
            self.high_water = max(self.high_water, min(done / total, 0.99))
            return round(self.high_water, 4)

    def message(self) -> str | None:
        with self.lock:
            active = [s for s in self.stages if s['status'] == 'active']
            if not active:
                return None
            stage = active[0]
            return f"{stage['label']} · {stage['detail']}" if stage.get('detail') else stage['label']

    def complete(self) -> None:
        with self.lock:
            self.finished = True
            self.flush(force=True, final=True)

    def flush(self, force: bool = False, final: bool = False) -> None:
        with self.lock:
            if not force and clock() - self.written < WRITE_INTERVAL:
                self.dirty = True
                return
            self.written, self.dirty = clock(), False
            values = {'stages': [dict(s) for s in self.stages], 'overall': self.overall()}
            message = self.message()
            if message and not final:
                values['progress'] = message
            derived = next((STATUS_OF[s['id']] for s in self.stages if s['status'] == 'active'), None)
            with self.store.lock:
                try:
                    job = self.store.get(self.job_id)
                except KeyError:
                    return
                if job['status'] not in BUSY_STATUSES and not final:
                    return  # a finished, failed or cancelled job is never overwritten by late progress
                if derived and not final:
                    values['status'] = derived
                self.store.save({**job, **values})

    # cancel and abort
    def request_cancel(self) -> None:
        self.cancelled.set()
        self.kill_processes()

    def abort(self) -> None:
        """A sibling step failed; stop quietly without marking the job cancelled."""
        self.aborted.set()
        self.kill_processes()

    def should_stop(self) -> bool:
        return self.cancelled.is_set() or self.aborted.is_set()

    def check(self) -> None:
        if self.should_stop():
            raise Cancelled()

    def register(self, proc) -> None:
        with self.lock:
            self.processes.add(proc)
        if self.should_stop():
            self.kill_processes()

    def kill_processes(self) -> None:
        with self.lock:
            procs = list(self.processes)
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.kill()
            except OSError:
                pass

    # joins
    def mark_done(self, part: str) -> bool:
        """Record a finished branch; True for exactly the call that completes the required set."""
        with self.lock:
            before = self.required <= self.completed
            self.completed.add(part)
            return not before and self.required <= self.completed


def stage_views(stages: list[dict], tracker: Tracker | None = None) -> list[dict]:
    """Public stage list with stall flags computed from the clock, not from a timer."""
    result, moment = [], clock()
    for stage in stages or []:
        limit = (tracker.stall_after.get(stage['id']) if tracker else None) or STALL_SECONDS
        stalled = (stage.get('status') == 'active' and stage.get('progress_at') is not None
                   and moment - stage['progress_at'] > limit)
        view = {key: stage.get(key) for key in ('id', 'label', 'status', 'fraction', 'detail', 'started_at', 'updated_at')}
        view['eta_seconds'] = stage.get('eta_seconds')
        view['stalled'] = bool(stalled)
        if stalled:
            view['stall_reason'] = stage.get('hint') or STALL_REASONS.get(stage['id'])
        result.append(view)
    return result
