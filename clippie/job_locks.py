"""Cross-process ownership for Studio jobs. Locks are released by the OS on process exit."""
import fcntl
import os
from pathlib import Path
import threading

_guard = threading.Lock()
_held = {}
_pid = os.getpid()


def acquire(root, job_id):
    global _pid
    path = Path(root).resolve() / 'studio-job-locks' / (job_id + '.lock')
    with _guard:
        if _pid != os.getpid():
            # Forked children must not consider the parent's registry their own.
            _held.clear()
            _pid = os.getpid()
        key = str(path)
        if key in _held:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open('a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        _held[key] = handle
        return True


def release(root, job_id):
    key = str(Path(root).resolve() / 'studio-job-locks' / (job_id + '.lock'))
    with _guard:
        handle = _held.pop(key, None)
        if handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
