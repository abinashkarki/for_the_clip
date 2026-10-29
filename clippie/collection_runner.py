"""One synchronous durable dispatch; no scheduler or provider selection."""
import logging
import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, TypedDict

LOG = logging.getLogger('clippie.collections')


class HandlerResult(TypedDict, total=False):
    result: Any
    actual_tokens: int
    actual_calls: int


class LeaseLost(RuntimeError):
    pass


@dataclass
class StageContext:
    store: Any
    task: dict
    lease_seconds: float
    _lost: threading.Event = field(default_factory=threading.Event)
    _processes: list = field(default_factory=list)
    _lock: Any = field(default_factory=threading.RLock)
    _renew_at: float = 0

    def _invalidate(self):
        self._lost.set()
        self._terminate_processes()

    def _terminate_processes(self):
        with self._lock:
            for process in self._processes:
                if process.poll() is None:
                    try:
                        process.terminate()
                        try:
                            process.wait(timeout=0.2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                    except ProcessLookupError:
                        pass

    def checkpoint(self):
        if self._lost.is_set():
            raise LeaseLost('lease_lost')
        try:
            with self._lock:
                if time.monotonic() >= self._renew_at:
                    self.store.renew_lease(self.task['id'], self.task['lease_token'],
                                          self.task['input_version'], self.lease_seconds)
                    self._renew_at = time.monotonic() + self.lease_seconds / 3
                else:
                    self.store.check_lease(self.task['id'], self.task['lease_token'], self.task['input_version'])
        except (ValueError, KeyError):
            self._invalidate()
            raise LeaseLost('lease_lost') from None

    def register_process(self, process):
        """Take ownership of a Popen child; terminate it if the lease is lost."""
        with self._lock:
            self._processes.append(process)
        self.checkpoint()
        return process


def run_one(store, handlers: Mapping[str, Callable[[StageContext], HandlerResult]],
            collection_id=None, lease_seconds=60, stages=None):
    """Claim at most one task and return its terminal task document, or None.

    Handlers cooperate via checkpoint and register_process. Python handlers cannot
    be forcibly stopped; their late output is fenced and known usage reconciled.
    Heartbeat owns only lease renewal and registered child cancellation.
    """
    task = store.claim_task(collection_id=collection_id, lease_seconds=lease_seconds,
                            **({'stages': stages} if stages is not None else {}))
    if task is None:
        return None
    context = StageContext(store, task, lease_seconds)
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(max(0.001, min(lease_seconds / 3, 0.2))):
            try:
                context.checkpoint()
            except Exception:
                context._invalidate()
                return

    thread = threading.Thread(target=heartbeat, name='collection-lease', daemon=True)
    thread.start()
    output = {}
    error = None
    detail = None
    try:
        context.checkpoint()
        if task.get('committed_revision'):
            revision = store.get_revision(task['committed_revision'])
            recovered = dict(revision['document'].get('editorial', {})) if task['stage'] == 'review_short' else {}
            # No invented usage on recovery: the previous unknown attempt stays charged.
            output = {'result': {**recovered, 'revision_id': revision['id'], 'adopted': True}}
        elif task['stage'] not in handlers:
            error = 'handler_unavailable'
        else:
            output = handlers[task['stage']](context)
            if not isinstance(output, dict) or 'result' not in output:
                output = {}
                error = 'handler_invalid_result'
            else:
                try:
                    json.dumps(output['result'], allow_nan=False)
                    for key in ('actual_tokens', 'actual_calls'):
                        if key in output:
                            value = output[key]
                            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                                raise ValueError('invalid usage')
                except (ValueError, TypeError):
                    output = {}
                    error = 'handler_invalid_result'
        context.checkpoint()
    except LeaseLost:
        error = 'lease_lost'
    except Exception as exception:
        category = getattr(exception, 'category', None)
        detail = f'{type(exception).__name__}: {exception}'[:300]
        LOG.warning('Collection task %s (%s) failed: %s', task.get('id'), task.get('stage'), detail, exc_info=True)
        allowed = {'provider_auth', 'provider_quota', 'provider_config', 'provider_service',
                   'provider_network', 'provider_timeout', 'provider_output_limit', 'provider_retry_budget',
                   'provider_invalid_output', 'provider_input_limit', 'provider_reservation', 'provider_deadline', 'editor_invalid_output', 'editor_version_changed'}
        error = category if category in allowed else 'handler_failed'
    finally:
        stop.set()
        thread.join()
        context._terminate_processes()
    usage = {k: output[k] for k in ('actual_tokens', 'actual_calls') if k in output}
    try:
        return store.finish_task(task['id'], task['lease_token'], task['input_version'],
                                 result=output.get('result'), error=error, error_detail=detail, **usage)
    except (ValueError, KeyError):
        context._invalidate()
        if usage:
            try:
                store.record_usage(task['id'], task['lease_token'], **usage)
            except (ValueError, KeyError):
                pass
        return next(t for t in store.list_tasks(task['collection_id']) if t['id'] == task['id'])
