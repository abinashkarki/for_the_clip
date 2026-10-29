"""Studio state in `<data dir>/studio.sqlite3`: brand kits and assets, uploads, jobs, exports, idempotency.

Documents are JSON rows like the other stores. Opening the store never changes jobs;
recovery is performed explicitly by the coordinator after checking ownership.
"""
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,100}$')
INTERRUPTED = 'The app stopped during this job. Start it again; your saved clip is unchanged.'


def valid_id(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class IdempotencyConflict(ValueError):
    """A request ID was reused for a different request."""


class StudioStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        from .storage_safety import assert_compatible
        assert_compatible(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'studio.sqlite3'
        self.lock = threading.RLock()
        with self._db() as db:
            for name in ('brand_kits', 'brand_assets', 'uploads', 'exports', 'project_options'):
                db.execute(f'CREATE TABLE IF NOT EXISTS {name} (id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS idempotency (scope TEXT NOT NULL, key TEXT NOT NULL, '
                       'fingerprint TEXT NOT NULL, status INTEGER NOT NULL, response TEXT NOT NULL, '
                       'PRIMARY KEY(scope, key))')
            db.execute('CREATE TABLE IF NOT EXISTS job_events (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, '
                       'at REAL NOT NULL, status TEXT, message TEXT, attempt INTEGER)')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA journal_mode=WAL')
        return _Transaction(db)

    # -- generic documents -------------------------------------------------
    def _get(self, table, identity):
        with self._db() as db:
            row = db.execute(f'SELECT document FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return json.loads(row[0])

    def _put(self, table, document):
        with self._db() as db:
            db.execute(f'INSERT INTO {table} VALUES (?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document',
                       (document['id'], _dump(document)))
        return document

    def _all(self, table):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute(f'SELECT document FROM {table} ORDER BY rowid')]

    # -- brand kits --------------------------------------------------------
    def list_kits(self):
        return self._all('brand_kits')

    def get_kit(self, kit_id):
        return self._get('brand_kits', kit_id)

    def create_kit(self, fields):
        stamp = time.time()
        return self._put('brand_kits', {'id': uuid.uuid4().hex, **fields, 'created_at': stamp, 'updated_at': stamp})

    def update_kit(self, kit_id, fields):
        with self.lock:
            kit = self.get_kit(kit_id)
            kit.update(fields, updated_at=time.time())
            return self._put('brand_kits', kit)

    def delete_kit(self, kit_id):
        with self.lock, self._db() as db:
            self.get_kit(kit_id)
            db.execute('DELETE FROM brand_kits WHERE id=?', (kit_id,))

    # -- brand assets, uploads, project options ----------------------------
    def add_asset(self, document):
        return self._put('brand_assets', {**document, 'created_at': time.time()})

    def get_asset(self, asset_id):
        return self._get('brand_assets', asset_id)

    def add_upload(self, document):
        return self._put('uploads', {**document, 'created_at': time.time()})

    def get_upload(self, source_id):
        return self._get('uploads', source_id)

    def set_project_options(self, project_id, options):
        return self._put('project_options', {'id': project_id, **options})

    def project_options(self, project_id):
        try:
            return self._get('project_options', project_id)
        except KeyError:
            return {}

    # -- jobs --------------------------------------------------------------
    def create_job(self, kind, clip_id=None, payload=None):
        from . import job_locks
        stamp = time.time()
        job = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'queued', 'progress': 0.0, 'error': None,
               'result': None, 'clip_id': clip_id, 'payload': payload or {}, 'created_at': stamp, 'updated_at': stamp,
               'attempt': 1}
        if not job_locks.acquire(self.root, job['id']):
            raise RuntimeError('Could not claim the new job.')
        try:
            with self._db() as db:
                db.execute('INSERT INTO jobs VALUES (?,?)', (job['id'], _dump(job)))
                db.execute('INSERT INTO job_events(job_id,at,status,message,attempt) VALUES (?,?,?,?,?)',
                           (job['id'], stamp, 'queued', 'Waiting to start', 1))
            return job
        except Exception:
            job_locks.release(self.root, job['id'])
            raise

    def get_job(self, job_id):
        return self._get('jobs', job_id)

    def update_job(self, job_id, **values):
        with self.lock, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = json.loads(row[0])
            previous = (job.get('status'), job.get('message'))
            job.update(values, updated_at=time.time())
            db.execute('UPDATE jobs SET document=? WHERE id=?', (_dump(job), job_id))
            if previous != (job.get('status'), job.get('message')):
                # Store only locally authored stage messages, never raw provider errors or secrets.
                message = {'queued': 'Waiting', 'running': 'Working', 'done': 'Completed',
                           'failed': 'Needs attention'}.get(job.get('status'), 'Updated')
                db.execute('INSERT INTO job_events(job_id,at,status,message,attempt) VALUES (?,?,?,?,?)',
                           (job_id, job['updated_at'], job.get('status'), message, job.get('attempt', 1)))
            return job

    def list_jobs(self):
        with self._db() as db:
            rows = db.execute('SELECT document FROM jobs').fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda job: job.get('created_at', 0), reverse=True)

    def job_events(self, job_id):
        with self._db() as db:
            return [{'at': r[0], 'status': r[1], 'message': r[2], 'attempt': r[3]} for r in db.execute(
                'SELECT at,status,message,attempt FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 20', (job_id,))]

    # -- exports (artifact kind studio_export) -----------------------------
    def add_export(self, document):
        return self._put('exports', {**document, 'kind': 'studio_export', 'created_at': time.time()})

    def get_export(self, artifact_id):
        return self._get('exports', artifact_id)

    def latest_export(self, clip_id, revision_id):
        matches = [item for item in self._all('exports')
                   if item['clip_id'] == clip_id and item['revision_id'] == revision_id]
        return matches[-1] if matches else None

    # -- idempotency -------------------------------------------------------
    def replay(self, scope, key, fingerprint):
        """Stored (status, body) for a repeated request, None for a new one."""
        with self._db() as db:
            row = db.execute('SELECT fingerprint, status, response FROM idempotency WHERE scope=? AND key=?',
                             (scope, key)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise IdempotencyConflict('This request_id was already used for a different request.')
        return row[1], json.loads(row[2])

    def remember(self, scope, key, fingerprint, status, body):
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO idempotency VALUES (?,?,?,?,?)',
                       (scope, key, fingerprint, status, _dump(body)))


class _Transaction:
    """Commit-on-success sqlite connection that always closes."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, kind, error, trace):
        try:
            if kind is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()
        return False
