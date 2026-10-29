"""Offline data-version gates and full local snapshots for desktop upgrades.

Mutation callers must hold desktop.instance_lock before importing stores. SQLite
uses its backup API (including committed WAL), not a raw live database copy.
Snapshots include the whole data folder except volatile cache and lock/control
temporaries; external referenced files and Keychain credentials are outside it.
"""
from __future__ import annotations

import ctypes
from contextlib import closing
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

FORMAT_VERSION = 1
VERSION_FILE = '.clippie-data.json'
RESTORE_FILE = '.clippie-restore.json'
LOCK_FILE = '.desktop.lock'
KNOWN_DATABASES = {'clippie.sqlite3', 'collections.sqlite3', 'studio.sqlite3',
                   'publish.sqlite3', 'models.sqlite3', 'telemetry.sqlite3'}
_STARTUPS = {}


class DataSafetyError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(path, progress=None):
    hashed, last = hashlib.sha256(), 0.0
    with path.open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            hashed.update(chunk)
            if progress and time.monotonic() - last > 1:
                progress('Verifying local recovery data…')
                last = time.monotonic()
    return hashed.hexdigest()


def root_key(root):
    return hashlib.sha256(os.fsencode(str(root.resolve()))).hexdigest()


def safe_root(root):
    root = Path(root).resolve()
    home = Path.home().resolve()
    if root == home or root in home.parents or str(root) in {'/Applications', '/System', '/Library', '/Volumes', '/usr', '/bin', '/sbin', '/opt', '/private', '/var', '/tmp'} or (root / '.git').exists():
        raise DataSafetyError('restore_failed', 'Choose a dedicated Clippie data folder before using recovery.')
    return root


def load_json(path):
    return json.loads(path.read_text())


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def version(root):
    path = Path(root) / VERSION_FILE
    if not path.exists():
        return {'format_version': 0, 'minimum_reader': 0, 'status': 'unversioned', 'build_id': ''}
    try:
        value = load_json(path)
        if not isinstance(value, dict) or any(type(value.get(key)) is not int or value[key] < 0 for key in ('format_version', 'minimum_reader')):
            raise ValueError()
        if value.get('status') not in {'ready', 'starting'} or not isinstance(value.get('build_id'), str):
            raise ValueError()
        if value.get('root_key') != root_key(Path(root)):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError):
        raise DataSafetyError('restore_failed', 'The local data-version record is invalid or this data folder was moved. Use Recovery at the original data location.') from None


def assert_compatible(root, *, allow_pending=False):
    value = version(root)
    if max(value['format_version'], value['minimum_reader']) > FORMAT_VERSION:
        raise DataSafetyError('data_too_new', 'This data folder was opened by a newer Clippie data format. Install the newer app or restore a compatible recovery copy.')
    if value['status'] == 'starting' and not allow_pending:
        if value.get('owner_pid') != os.getpid():
            raise DataSafetyError('upgrade_interrupted', 'Initialization was interrupted. Open this app with explicit Retry or restore a recovery copy before opening stores.')
        require_lock(Path(root))
    return value


def require_lock(root):
    # A second independent flock descriptor must fail, and the lock's PID must
    # be ours. Presence of a file alone is not ownership.
    import fcntl
    try:
        descriptor = os.open(root / LOCK_FILE, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        try:
            if os.read(descriptor, 64).decode().strip() != str(os.getpid()):
                raise ValueError()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            raise ValueError()
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        raise DataSafetyError('backup_failed', 'Stop other Clippie processes and acquire the data-folder lock before recovery.') from None


def recovery_store(root):
    store = root.parent / (root.name + '.recovery')
    if store.is_symlink():
        raise DataSafetyError('backup_failed', 'The recovery folder must not be a symbolic link.')
    return store


def build_identity():
    package = Path(__file__).resolve().parent
    hashed = hashlib.sha256()
    for path in sorted(package.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix in {'.py', '.json'}:
            hashed.update(str(path.relative_to(package)).encode() + b'\0' + path.read_bytes())
    return hashed.hexdigest()


def clone_or_copy(source, destination, progress=None):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform == 'darwin':
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.clonefile(os.fsencode(source), os.fsencode(destination), 1)  # CLONE_NOFOLLOW
        if result == 0:
            return
        error = ctypes.get_errno()
        if error not in {errno.ENOTSUP, errno.EXDEV, errno.EINVAL, errno.ENOSYS}:
            raise OSError(error, 'Could not clone local recovery data.')
    last = 0.0
    with source.open('rb') as incoming, destination.open('xb') as outgoing:
        while chunk := incoming.read(4 * 1024 * 1024):
            outgoing.write(chunk)
            if progress and time.monotonic() - last > 1:
                progress('Copying local recovery data…')
                last = time.monotonic()
        outgoing.flush()
        os.fsync(outgoing.fileno())
    shutil.copystat(source, destination, follow_symlinks=False)


def atomic_swap(first, second):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        result = libc.renamex_np(os.fsencode(first), os.fsencode(second), 2)  # RENAME_SWAP
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    else:
        raise DataSafetyError('restore_failed', 'Atomic folder replacement is unavailable on this filesystem. The current data has not been replaced.')
    if result:
        raise DataSafetyError('restore_failed', 'This filesystem could not atomically replace the data folder. The current data has not been replaced.')


def inventory(root, *, exclude=True):
    result = {}
    def walk(folder):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root).as_posix()
            if exclude and (relative in {LOCK_FILE, VERSION_FILE + '.tmp', RESTORE_FILE + '.tmp'} or relative == 'cache' or relative.startswith('cache/')):
                continue
            details = path.lstat()
            if path.is_symlink():
                if path.name in KNOWN_DATABASES:
                    raise DataSafetyError('backup_failed', 'Linked databases cannot be snapshotted safely. Keep databases inside the Clippie data folder.')
                result[relative] = {'kind': 'link', 'target': os.readlink(path)}
            elif stat.S_ISDIR(details.st_mode):
                result[relative] = {'kind': 'directory'}
                walk(path)
            elif stat.S_ISREG(details.st_mode):
                result[relative] = {'kind': 'file', 'size': details.st_size, 'mtime_ns': details.st_mtime_ns, 'inode': details.st_ino}
            else:
                raise DataSafetyError('backup_failed', 'The data folder contains an unsupported special file. No upgrade was started.')
    walk(root)
    return result


def is_database(path):
    with path.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\0'


def snapshot(root, *, reason, progress=None, raw=False):
    root = safe_root(root)
    require_lock(root)
    try:
        current = version(root)
    except DataSafetyError:
        if not raw:
            raise
        current = {'format_version': FORMAT_VERSION, 'build_id': ''}
    store = recovery_store(root)
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = uuid4().hex
    temporary = store / ('.pending-' + identity)
    temporary.mkdir(mode=0o700)
    payload = temporary / 'payload'
    payload.mkdir(mode=0o700)
    connections = {}
    before = inventory(root)
    database_names = set() if raw else {name for name, item in before.items() if item['kind'] == 'file' and is_database(root / name)}
    if not raw and any(name in before and name not in database_names for name in KNOWN_DATABASES):
        shutil.rmtree(temporary)
        raise DataSafetyError('backup_failed', 'A local database is invalid. The app has not changed it; use Recovery or repair a copy before continuing.')
    sidecars = {name + suffix for name in database_names for suffix in ('-wal', '-shm', '-journal')}
    before = {name: item for name, item in before.items() if name not in sidecars}
    stamp = 0.0
    try:
        for name in database_names:
            source = sqlite3.connect((root / name).as_uri() + '?mode=ro', uri=True, timeout=10)
            connections[name] = (source, source.execute('PRAGMA data_version').fetchone()[0])
        entries = []
        for name, item in before.items():
            if progress and time.monotonic() - stamp > 1:
                progress('Creating a local recovery copy before opening your data…')
                stamp = time.monotonic()
            source, target = root / name, payload / name
            if item['kind'] == 'directory':
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                entries.append({'path': name, 'kind': 'directory'})
            elif item['kind'] == 'link':
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.symlink(item['target'], target)
                entries.append({'path': name, **item})
            else:
                if name in connections:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with closing(sqlite3.connect(target)) as destination:
                        connections[name][0].backup(destination, pages=256,
                            progress=(lambda *_: progress('Copying the local database…')) if progress else None)
                        destination.execute('PRAGMA journal_mode=DELETE')
                        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise sqlite3.DatabaseError()
                        destination.commit()
                else:
                    clone_or_copy(source, target, progress)
                entries.append({'path': name, 'kind': 'database' if name in connections else 'file',
                                'size': target.stat().st_size, 'sha256': digest(target, progress)})
        after = {name: item for name, item in inventory(root).items() if name not in sidecars}
        if before != after or any(source.execute('PRAGMA data_version').fetchone()[0] != revision for source, revision in connections.values()):
            raise DataSafetyError('backup_failed', 'Data changed while creating its recovery copy. Quit all other Clippie processes and retry.')
        record = {'backup_format': 1, 'id': identity, 'root_key': root_key(root), 'created_at': utc_now(),
                  'format_version': current['format_version'], 'build_id': current['build_id'], 'reason': reason,
                  'rescue_only': raw,
                  'scope': 'whole local data folder except volatile cache and process locks',
                  'exclusions': ['cache/', LOCK_FILE, 'external referenced files', 'Keychain', 'remote actions'],
                  'entries': entries, 'bytes': sum(item.get('size', 0) for item in entries)}
        atomic_json(temporary / 'manifest.json', record)
        os.replace(temporary, store / identity)
        return identity
    except DataSafetyError:
        raise
    except Exception:
        raise DataSafetyError('backup_failed', 'Could not complete a verified recovery copy. Check free space and data-folder access before retrying.') from None
    finally:
        for source, _ in connections.values():
            source.close()
        if temporary.exists():
            shutil.rmtree(temporary)


def checked_snapshot(root, identity, *, progress=None):
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
        raise DataSafetyError('restore_failed', 'Choose a valid recovery copy from this data folder.')
    path = recovery_store(root) / identity
    try:
        if path.is_symlink() or (path / 'payload').is_symlink():
            raise ValueError()
        record = load_json(path / 'manifest.json')
        if record.get('rescue_only') or record['backup_format'] != 1 or record['id'] != identity or record['root_key'] != root_key(root) or type(record['format_version']) is not int or not 0 <= record['format_version'] <= FORMAT_VERSION:
            raise ValueError()
        names = set()
        stamp = 0.0
        for item in record['entries']:
            name = item['path']
            if Path(name).is_absolute() or '..' in Path(name).parts or name in names or name == LOCK_FILE:
                raise ValueError()
            names.add(name)
            target = path / 'payload' / name
            # A parent symlink must not redirect validation or later restore.
            if any(parent.is_symlink() for parent in target.parents if parent.is_relative_to(path / 'payload')):
                raise ValueError()
            if progress and time.monotonic() - stamp > 1:
                progress('Verifying the selected recovery copy…')
                stamp = time.monotonic()
            if item['kind'] == 'link':
                if not target.is_symlink() or os.readlink(target) != item['target']:
                    raise ValueError()
            elif item['kind'] == 'directory':
                if target.is_symlink() or not target.is_dir():
                    raise ValueError()
            elif item['kind'] in {'file', 'database'}:
                if target.is_symlink() or not target.is_file() or target.stat().st_size != item['size'] or digest(target, progress) != item['sha256']:
                    raise ValueError()
                if item['kind'] == 'database':
                    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
                        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError()
            else:
                raise ValueError()
        if set(inventory(path / 'payload', exclude=False)) != names:
            raise ValueError()
        return path, record
    except (KeyError, ValueError, OSError, sqlite3.Error, TypeError):
        raise DataSafetyError('restore_failed', 'This recovery copy is incomplete, changed, incompatible or belongs to another data folder. Current data was not replaced.') from None


def restore(root, identity, *, progress=None):
    root = safe_root(root)
    require_lock(root)
    path, record = checked_snapshot(root, identity, progress=progress)
    try:
        rescue = snapshot(root, reason='before-restore', progress=progress)
    except DataSafetyError:
        # Recovery must also work when current databases/version metadata are
        # damaged. Preserve exact local files, including WAL, for manual repair.
        # This rescue is deliberately ineligible for automatic restoration.
        rescue = snapshot(root, reason='damaged-state-rescue', progress=progress, raw=True)
    store = recovery_store(root)
    stage = store / ('.restore-' + uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        for item in record['entries']:
            source, target = path / 'payload' / item['path'], stage / item['path']
            if item['kind'] == 'directory':
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif item['kind'] == 'link':
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.symlink(item['target'], target)
            else:
                clone_or_copy(source, target, progress)
        # Both directory names retain the same live lock inode across the atomic
        # exchange. Another launcher can never acquire a newly created lock.
        os.link(root / LOCK_FILE, stage / LOCK_FILE)
        atomic_json(stage / RESTORE_FILE, {'restored_at': utc_now(), 'backup_id': identity,
                                         'rescue_id': rescue, 'background_resume_disabled': True})
        if progress:
            progress('Replacing local data; the current state has a rescue copy…')
        atomic_swap(root, stage)
        os.replace(stage, store / ('.retired-' + uuid4().hex))
        return {'event': 'restored', 'backup_id': identity, 'rescue_id': rescue}
    except DataSafetyError:
        raise
    except Exception:
        raise DataSafetyError('restore_failed', 'Could not finish local restoration. The recovery folder retains its copies; use Recovery before continuing.') from None
    # Never delete a stage here: if the swap succeeded it contains the old data.


def recovery_info(root):
    root = safe_root(root)
    try:
        current = version(root)
    except DataSafetyError:
        current = {'format_version': -1, 'status': 'invalid'}
    backups = []
    store = recovery_store(root)
    if store.exists():
        for path in store.iterdir():
            if not re.fullmatch('[a-f0-9]{32}', path.name) or path.is_symlink():
                continue
            try:
                record = load_json(path / 'manifest.json')
                if (not isinstance(record, dict) or record.get('root_key') != root_key(root)
                        or record.get('id') != path.name or record.get('backup_format') != 1
                        or not isinstance(record.get('created_at'), str)
                        or not isinstance(record.get('build_id'), str)
                        or type(record.get('bytes')) is not int or record['bytes'] < 0
                        or type(record.get('format_version')) is not int or record['format_version'] < 0):
                    continue
                backups.append({key: record[key] for key in ('id', 'created_at', 'format_version', 'build_id', 'bytes')}
                               | {'can_restore': not record.get('rescue_only') and record['format_version'] <= FORMAT_VERSION})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return {'event': 'recovery_info', 'current': {key: current[key] for key in ('format_version', 'status')},
            'backups': sorted(backups, key=lambda item: item['created_at'], reverse=True),
            'message': 'Recovery copies cover this local data folder. External files, Keychain and remote actions are not restored.'}


def prepare_startup(root, *, build_id=None, retry=False, progress=None):
    root = safe_root(root)
    require_lock(root)
    current = assert_compatible(root, allow_pending=True)
    build_id = build_id or build_identity()
    if current['status'] == 'starting':
        if not retry or current['build_id'] != build_id:
            raise DataSafetyError('upgrade_interrupted', 'A previous initialization did not finish. Retry this same app explicitly or restore a recovery copy.')
        current = {**current, 'owner_pid': os.getpid()}
        atomic_json(root / VERSION_FILE, current)
        _STARTUPS[str(root)] = current
        return current
    if current['status'] == 'ready' and current['build_id'] == build_id:
        return current
    backup_id = snapshot(root, reason='before-upgrade', progress=progress) if inventory(root) else None
    pending = {'format_version': FORMAT_VERSION, 'minimum_reader': FORMAT_VERSION, 'root_key': root_key(root),
               'status': 'starting', 'build_id': build_id, 'backup_id': backup_id, 'started_at': utc_now(), 'owner_pid': os.getpid()}
    atomic_json(root / VERSION_FILE, pending)
    _STARTUPS[str(root)] = pending
    return pending


def complete_startup(root):
    root = Path(root).resolve()
    pending = _STARTUPS.get(str(root))
    if pending is not None:
        require_lock(root)
        atomic_json(root / VERSION_FILE, {**pending, 'status': 'ready', 'ready_at': utc_now()})
        _STARTUPS.pop(str(root), None)


def restore_notice(root):
    try:
        value = load_json(Path(root) / RESTORE_FILE)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def background_resume_disabled(root):
    return restore_notice(root).get('background_resume_disabled') is True


def quarantine_restored_work(root):
    notice = restore_notice(root)
    if not notice or notice.get('quarantine_applied'):
        return
    if (Path(root) / 'collections.sqlite3').exists():
        from .collection_store import CollectionStore
        CollectionStore(root).pause_after_restore()
    if (Path(root) / 'publish.sqlite3').exists():
        from .publish.store import PublishStore
        PublishStore(root).pause_after_restore()
    atomic_json(Path(root) / RESTORE_FILE, {**notice, 'quarantine_applied': True})


def acknowledge_restore(root):
    path = Path(root) / RESTORE_FILE
    value = restore_notice(root)
    if value:
        atomic_json(path, {**value, 'background_resume_disabled': False, 'reviewed_at': utc_now()})
    return restore_notice(root)
