"""Explicit local traces. No HTTP auto-instrumentation, network exporter or content capture.

The application store remains authoritative. A bounded queue drops telemetry rather
than blocking work; counters make those gaps visible. Only allowlisted scalar
metadata reaches SQLite, including at the OTel export boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode

OBSERVATION = ContextVar('clippie_observation', default='user')
_CURRENT = ContextVar('clippie_telemetry_run', default=None)
_RECORDERS = {}
_RECORDERS_LOCK = threading.Lock()
# Free text, filenames, URLs, query/body data, genre labels and exception messages
# are deliberately absent. Fixed enum values and internal IDs are validated too.
_ENUMS = {
    'generation_outcome': {'completed', 'needs_attention', 'cancelled', 'pending'},
    'error_type': {'ValueError','TypeError','KeyError','RuntimeError','TimeoutError','TimeoutExpired','OSError','FileNotFoundError','PermissionError','HTTPException','CalledProcessError','JSONDecodeError','ConnectError','ReadTimeout','HTTPStatusError','MemoryError','GeminiAPIError','Exception'},
    'error_code': {'timeout','invalid_output','io_error','request_rejected','stage_failed','provider_quota','provider_auth','provider_config','provider_timeout','provider_network','provider_service','provider_output_limit','provider_retry_budget'},
    'workflow': {'collection_prepare', 'collection_generate', 'production', 'replan', 'render', 'ingest', 'selection', 'export', 'suggestion', 'index', 'search'},
    'observation': {'user', 'qa', 'synthetic'},
    'planner': {'baseline', 'local', 'deepseek', 'gemini', 'heuristic'},
    'transcriber': {'auto', 'mlx', 'mlx-whisper', 'groq', 'import', 'none', 'pending'},
    'cache': {'hit', 'miss', 'unknown'}, 'cache_kind': {'evidence', 'recommendation', 'index', 'transcript'},
    'cache_state': {'warm', 'cold', 'mixed', 'unknown'},
    'rating': {'keep', 'revise', 'reject'}, 'kind': {'video', 'subtitles', 'manifest', 'report'},
    'field': {'brief', 'style_id', 'genre', 'target_duration', 'planner', 'use_embeddings'},
    'finish_reason': {'stop', 'length', 'eos'},
    'thinking_level': {'low','medium','high'},
    'reason': {'invalid_output', 'runtime_budget', 'request_rejected', 'model_unavailable', 'busy', 'interrupted'},
}
_NUMBERS = set('reserved_tokens reserved_calls charged_tokens charged_calls revision style_version defaults_version source_count target_duration actual_duration primary_source_count used_source_count shots_planned broll_overlays passages_considered beats_planned word_count item_count text_count image_count prompt_tokens completion_tokens total_tokens thought_tokens cached_tokens max_tokens provider_requests local_editor_requests peak_memory_gb generation_tps model_seconds output_bytes render_seconds source_seconds http_status attempt retry_count retry_wait_seconds changed_clip_count changed_broll_count previous_clip_count current_clip_count caption_changes framing_changes timeline_duration_delta'.split())
_BOOLS = {'use_embeddings', 'auto_render', 'captions', 'synthetic', 'accepted_defaults'}
_IDS = {'batch_id', 'collection_id', 'short_id', 'revision_id', 'task_id', 'attempt_id', 'source_id', 'production_id', 'previous_run_id', 'suggestion_run_id', 'run_id', 'export_id', 'diagnostic_id', 'event_id', 'render_id'}
_VERSION = {'style_id', 'model_id', 'model_revision', 'runtime_version'}
_SAFE_ID = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
_SAFE_VERSION = re.compile(r'^[A-Za-z0-9_.:/-]{1,120}$')
STAGES = {'collection_task', 'source_evidence', 'queue', 'ingest', 'preview', 'transcription', 'selection', 'recommendation', 'brief_writing', 'index', 'retrieval', 'planning', 'supporting_shots', 'render', 'model', 'model_worker', 'export', 'provider_request'}
EVENTS = {'recommendation_changed', 'recommendation_alternative', 'recommendation_reset', 'recommendation_used', 'playing', 'download_clicked', 'timeline_saved', 'review_saved', 'planner_repair', 'request_rejected', 'provider_retry', 'provider_exhausted'}


def safe_attributes(values):
    result = {}
    if not isinstance(values, dict):
        return result
    for key, value in values.items():
        if key == 'source_ids' and isinstance(value, (list, tuple)) and len(value) <= 12 and all(isinstance(item, str) and _SAFE_ID.fullmatch(item) for item in value):
            result[key] = list(value)
        elif key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            result[key] = value
        elif key in _NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = value
        elif key in _BOOLS and isinstance(value, bool):
            result[key] = value
        elif key in _IDS and isinstance(value, str) and _SAFE_ID.fullmatch(value):
            result[key] = value
        elif key in _VERSION and isinstance(value, str) and _SAFE_VERSION.fullmatch(value) and '://' not in value and not value.startswith('/'):
            result[key] = value
    return result


def error_attributes(error):
    # Never use str(error), traceback or library-supplied exception attributes.
    kind = type(error).__name__
    known = {'ValueError','TypeError','KeyError','RuntimeError','TimeoutError','TimeoutExpired','OSError','FileNotFoundError','PermissionError','HTTPException','CalledProcessError','JSONDecodeError','ConnectError','ReadTimeout','HTTPStatusError','MemoryError','GeminiAPIError'}
    kind = kind if kind in known else 'Exception'
    if isinstance(error, (TimeoutError,)) or kind == 'TimeoutExpired':
        code = 'timeout'
    elif isinstance(error, (ValueError, TypeError, KeyError)):
        code = 'invalid_output'
    elif isinstance(error, OSError):
        code = 'io_error'
    elif kind == 'HTTPException':
        code = 'request_rejected'
    else:
        code = 'stage_failed'
    attrs = {'error_type': kind, 'error_code': code}
    if kind == 'GeminiAPIError':
        category = getattr(error, 'category', None)
        if category in {'quota','auth','config','timeout','network','output_limit','service','retry_budget'}:
            attrs['error_code'] = 'provider_'+category
        status = getattr(error, 'status_code', None)
        if isinstance(status,int) and 100 <= status <= 599:
            attrs['http_status'] = status
    return safe_attributes(attrs)


def _span_row(span, ended=True):
    ctx = span.get_span_context() if hasattr(span, 'get_span_context') else span.context
    parent = span.parent
    return {'id': f'{ctx.span_id:016x}', 'trace_id': f'{ctx.trace_id:032x}',
            'parent_id': f'{parent.span_id:016x}' if parent else None,
            'name': span.name if span.name in STAGES or span.name == 'workflow' else 'model_worker',
            'start_ns': span.start_time, 'end_ns': span.end_time if ended else None,
            'status': ('failed' if span.status.status_code == StatusCode.ERROR else 'success') if ended else 'running',
            'attributes': safe_attributes(dict(span.attributes or {}))}


class LocalExporter(SpanExporter):
    def __init__(self, recorder):
        self.recorder = recorder

    def export(self, spans):
        for span in spans:
            self.recorder.enqueue('span', _span_row(span))
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class StartProcessor(SpanProcessor):
    def __init__(self, recorder):
        self.recorder = recorder

    def on_start(self, span, parent_context=None):
        self.recorder.enqueue('span', _span_row(span, False))

    def on_end(self, span):
        pass


@dataclass
class Run:
    recorder: 'Recorder'
    id: str
    span: object
    data: dict
    caches: set = field(default_factory=set)
    failed: bool = False

    def update(self, **attrs):
        clean = safe_attributes(attrs)
        self.data['attributes'].update(clean)
        try:
            for key, value in clean.items():
                self.span.set_attribute(key, value)
        except Exception:
            self.recorder.counters['runtime_errors'] += 1
        self.recorder.enqueue('run', dict(self.data, attributes=dict(self.data['attributes'])))


class Recorder:
    def __init__(self, root, capacity=1024):
        from .storage_safety import assert_compatible
        assert_compatible(Path(root))
        self.path = Path(root)/'telemetry.sqlite3'
        self.session = uuid4().hex
        self.queue = queue.Queue(maxsize=capacity)
        self.counters = {'queued': 0, 'written': 0, 'dropped': 0, 'storage_errors': 0, 'read_errors': 0, 'runtime_errors': 0, 'pruned_runs': 0, 'pruned_events': 0, 'pruned_spans': 0}
        self.closed = False
        self.stop = threading.Event()
        self.last_error = None
        self.tracer_provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
        self.tracer_provider.add_span_processor(StartProcessor(self))
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(LocalExporter(self)))
        self.tracer = self.tracer_provider.get_tracer('clippie.workflows', '1')
        self.thread = threading.Thread(target=self._writer, name='clippie-telemetry', daemon=True)
        self.thread.start()
        self.enqueue('initialize', {})

    def enqueue(self, kind, value):
        if self.closed:
            self.counters['dropped'] += 1
            return False
        try:
            self.queue.put_nowait((kind, value))
            self.counters['queued'] += 1
            return True
        except queue.Full:
            self.counters['dropped'] += 1
            return False

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=.15)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=150')
        return conn

    def _initialize(self, conn):
        conn.executescript('''
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, trace_id TEXT, session_id TEXT,
            workflow TEXT, observation TEXT, status TEXT, created_ns INTEGER, ended_ns INTEGER, attributes TEXT);
          CREATE INDEX IF NOT EXISTS runs_recent ON runs(created_ns DESC);
          CREATE TABLE IF NOT EXISTS spans(id TEXT PRIMARY KEY, trace_id TEXT, parent_id TEXT,
            name TEXT, start_ns INTEGER, end_ns INTEGER, status TEXT, attributes TEXT);
          CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id, start_ns);
          CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, run_id TEXT, name TEXT,
            observation TEXT, at_ns INTEGER, attributes TEXT);
          CREATE INDEX IF NOT EXISTS events_recent ON events(at_ns DESC);
        ''')
        interrupted = time.time_ns()
        conn.execute("UPDATE runs SET status='interrupted', ended_ns=? WHERE status IN ('queued','running') AND session_id!=?", (interrupted, self.session))
        conn.execute("UPDATE spans SET status='interrupted',end_ns=? WHERE status='running' AND trace_id IN (SELECT trace_id FROM runs WHERE status='interrupted')", (interrupted,))
        conn.commit()

    def _write(self, conn, kind, row):
        attrs = json.dumps(safe_attributes(row.get('attributes')), separators=(',', ':'), allow_nan=False)
        if kind == 'run':
            conn.execute('INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?)',
                         (row['id'], row['trace_id'], self.session, row['workflow'], row['observation'], row['status'], row['created_ns'], row.get('ended_ns'), attrs))
        elif kind == 'span':
            conn.execute('INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?)',
                         (row['id'], row['trace_id'], row['parent_id'], row['name'], row['start_ns'], row['end_ns'], row['status'], attrs))
        elif kind == 'event':
            conn.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)',
                         (row['id'], row.get('run_id'), row['name'], row['observation'], row['at_ns'], attrs))
        conn.commit()

    def _prune(self, conn):
        # Metadata only: no media/model/document/revision files are removed.
        cutoff = time.time_ns()-30*86400*10**9
        for table, column, cap in [('runs','created_ns',5000),('events','at_ns',20000),('spans','start_ns',50000)]:
            active = " AND status NOT IN ('queued','running')" if table == 'runs' else " AND status!='running'" if table == 'spans' else ''
            deleted = conn.execute(f'DELETE FROM {table} WHERE {column}<?'+active, (cutoff,)).rowcount
            deleted += conn.execute(f'DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE 1=1'+active+f' ORDER BY {column} DESC LIMIT -1 OFFSET ?)', (cap,)).rowcount
            self.counters['pruned_'+table] += deleted
        conn.execute('DELETE FROM spans WHERE end_ns IS NOT NULL AND trace_id NOT IN (SELECT trace_id FROM runs)')
        conn.commit()

    def _writer(self):
        conn = None
        while not self.stop.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if item is None:
                    break
                if conn is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    conn = self._connect()
                    self._initialize(conn)
                self._write(conn, *item)
                self.counters['written'] += 1
                if self.counters['written'] % 200 == 0 or item[0] == 'initialize':
                    self._prune(conn)
                self.last_error = None
            except Exception:
                # Includes disk-full, permissions, corrupt DB, locked DB and adapter
                # failures. No payload is logged; the production task never waits.
                self.counters['storage_errors'] += 1
                self.counters['dropped'] += 1
                self.last_error = 'local_storage_unavailable'
                if conn:
                    conn.close()
                    conn = None
            finally:
                self.queue.task_done()
        if conn:
            conn.close()

    def flush(self, timeout=2):
        deadline = time.monotonic()+timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.005)
        return not self.queue.unfinished_tasks

    def shutdown(self):
        if self.closed:
            return
        self.tracer_provider.shutdown()
        self.flush()
        self.closed = True
        self.stop.set()
        self.thread.join(timeout=.5)

    def read(self, sql, params=()):
        try:
            conn = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.15)
            conn.row_factory = sqlite3.Row
            try:
                return [dict(row) for row in conn.execute(sql, params)]
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            self.counters['read_errors'] += 1
            return []

    def start(self, workflow, **attrs):
        workflow = workflow if workflow in _ENUMS['workflow'] else 'production'
        identity = uuid4().hex
        attrs = safe_attributes({**attrs, 'run_id': identity})
        span = self.tracer.start_span('workflow', context=trace.set_span_in_context(trace.INVALID_SPAN), attributes=attrs)
        data = {'id': identity, 'trace_id': f'{span.get_span_context().trace_id:032x}', 'workflow': workflow,
                'observation': 'synthetic' if attrs.get('synthetic') else OBSERVATION.get(), 'status': 'queued', 'created_ns': time.time_ns(), 'attributes': attrs}
        run = Run(self, identity, span, data)
        self.enqueue('run', dict(data, attributes=dict(attrs)))
        return run

    @contextmanager
    def execute(self, run):
        token = _CURRENT.set(run)
        origin_token = OBSERVATION.set(run.data['observation'])
        attached = None
        try:
            try:
                from opentelemetry import context
                attached = context.attach(trace.set_span_in_context(run.span))
                q = self.tracer.start_span('queue', start_time=run.data['created_ns'])
                q.end()
                run.data['status'] = 'running'
                run.update()
            except Exception:
                self.counters['runtime_errors'] += 1
            try:
                yield run
            except BaseException as error:
                failure(error)
                raise
            finally:
                try:
                    run.data['ended_ns'] = time.time_ns()
                    run.data['status'] = 'failed' if run.failed else 'success'
                    run.update(cache_state=('mixed' if len(run.caches) > 1 else 'warm' if run.caches == {'hit'} else 'cold' if run.caches == {'miss'} else 'unknown'))
                    run.span.end()
                except Exception:
                    self.counters['runtime_errors'] += 1
        finally:
            if attached is not None:
                try:
                    context.detach(attached)
                except Exception:
                    self.counters['runtime_errors'] += 1
            _CURRENT.reset(token)
            OBSERVATION.reset(origin_token)


def recorder(root=None):
    if root is None:
        from .app import STORE
        root = STORE.root
    key = str(Path(root).resolve())
    with _RECORDERS_LOCK:
        item = _RECORDERS.get(key)
        if item is None or item.closed:
            item = _RECORDERS[key] = Recorder(root)
    return item


def shutdown(root=None):
    with _RECORDERS_LOCK:
        items = list(_RECORDERS.values()) if root is None else [_RECORDERS.get(str(Path(root).resolve()))]
    for item in items:
        if item:
            try:
                item.shutdown()
            except Exception:
                item.counters['runtime_errors'] += 1


def begin(workflow, **attrs):
    try:
        return recorder().start(workflow, **attrs)
    except Exception:
        return None


def submit(executor, workflow, function, *args, telemetry_run=None, **attrs):
    run = telemetry_run or begin(workflow, **attrs)
    def work():
        if run is None:
            return function(*args)
        with run.recorder.execute(run):
            return function(*args)
    try:
        return executor.submit(work)
    except BaseException as error:
        if run:
            with run.recorder.execute(run):
                failure(error)
        raise


@contextmanager
def operation(workflow, **attrs):
    run = begin(workflow, **attrs)
    if run is None:
        yield None
    else:
        with run.recorder.execute(run):
            yield run


@contextmanager
def stage(name, fatal=True, **attrs):
    run = _CURRENT.get()
    span = trace.INVALID_SPAN
    attached = None
    if run and name in STAGES:
        try:
            from opentelemetry import context
            span = run.recorder.tracer.start_span(name, attributes=safe_attributes(attrs))
            attached = context.attach(trace.set_span_in_context(span))
        except Exception:
            run.recorder.counters['runtime_errors'] += 1
    try:
        yield span
    except BaseException as error:
        try:
            span.set_attributes(error_attributes(error))
            span.set_status(Status(StatusCode.ERROR))
        except Exception:
            if run:
                run.recorder.counters['runtime_errors'] += 1
        if fatal:
            failure(error)
        raise
    finally:
        try:
            if attached is not None:
                context.detach(attached)
            span.end()
        except Exception:
            if run:
                run.recorder.counters['runtime_errors'] += 1


def attributes(**attrs):
    try:
        clean = safe_attributes(attrs)
        trace.get_current_span().set_attributes(clean)
        run = _CURRENT.get()
        if run and 'generation_outcome' in clean:
            run.update(generation_outcome=clean['generation_outcome'])
        if run and 'cache' in clean and clean['cache'] in {'hit', 'miss'}:
            run.caches.add(clean['cache'])
    except Exception:
        pass


def update(**attrs):
    try:
        run = _CURRENT.get()
        if run:
            run.update(**attrs)
    except Exception:
        pass


def failure(error):
    try:
        run = _CURRENT.get()
        if run:
            run.failed = True
            run.update(**error_attributes(error))
            run.span.set_status(Status(StatusCode.ERROR))
    except Exception:
        pass


def event(name, run_id=None, observation=None, event_id=None, **attrs):
    if name not in EVENTS:
        return
    try:
        active = _CURRENT.get()
        rec = active.recorder if active else recorder()
        rec.enqueue('event', {'id': event_id or uuid4().hex, 'run_id': run_id or (active.id if active else None),
            'name': name, 'observation': observation if observation in _ENUMS['observation'] else OBSERVATION.get(),
            'at_ns': time.time_ns(), 'attributes': safe_attributes(attrs)})
    except Exception:
        pass


def propagation():
    """Only W3C trace IDs travel to the isolated model; never baggage or content."""
    ctx = trace.get_current_span().get_span_context()
    return f'00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01' if ctx.is_valid else None


def instrument(name, fatal=True):
    from functools import wraps
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name, fatal=fatal):
                return function(*args, **kwargs)
        return wrapped
    return decorate
