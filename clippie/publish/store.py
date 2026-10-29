"""Publishing state in `<data dir>/publish.sqlite3`.

Tables: channel_accounts (token *references* only), posts (each tied to the exact clip revision and export
artifact it publishes), metric_snapshots, retention_curves, settings, oauth_states. Secrets never enter SQLite.
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

STATUSES = ('draft', 'scheduled', 'uploading', 'published', 'failed')
POST_JSON = {'hashtags', 'warnings'}
POST_COLUMNS = ('id', 'platform', 'channel_id', 'short_id', 'revision_id', 'artifact_id', 'render_job_id', 'title',
                'description', 'hashtags', 'status', 'scheduled_at', 'published_at', 'external_id', 'url', 'error',
                'warnings', 'duration', 'upload_session', 'attempts', 'next_attempt_at', 'poll_stage',
                'next_poll_at', 'created_at', 'updated_at', 'project_id', 'project_name')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS channel_accounts (
  id TEXT PRIMARY KEY, platform TEXT NOT NULL, provider TEXT NOT NULL, display_name TEXT NOT NULL,
  external_id TEXT, scopes TEXT NOT NULL DEFAULT '[]', token_ref TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, platform TEXT NOT NULL, channel_id TEXT NOT NULL, short_id TEXT NOT NULL,
  revision_id TEXT NOT NULL, artifact_id TEXT, render_job_id TEXT, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  hashtags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL, scheduled_at REAL, published_at REAL, external_id TEXT,
  url TEXT, error TEXT, warnings TEXT NOT NULL DEFAULT '[]', duration REAL, upload_session TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL, poll_stage INTEGER NOT NULL DEFAULT 0,
  next_poll_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  project_id TEXT, project_name TEXT);
CREATE INDEX IF NOT EXISTS posts_short ON posts (short_id);
CREATE INDEX IF NOT EXISTS posts_status ON posts (status);
CREATE TABLE IF NOT EXISTS metric_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL, fetched_at REAL NOT NULL, source TEXT NOT NULL DEFAULT 'local',
  metrics TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS snapshots_post ON metric_snapshots (post_id, fetched_at);
CREATE TABLE IF NOT EXISTS retention_curves (
  id INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL, fetched_at REAL NOT NULL, points TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS retention_post ON retention_curves (post_id, fetched_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY, platform TEXT NOT NULL, verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS post_events (
  id INTEGER PRIMARY KEY, post_id TEXT NOT NULL, at REAL NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL);
'''
INTERRUPTED = 'The app stopped during this upload. It will resume automatically.'


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class PublishStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        from ..storage_safety import assert_compatible
        assert_compatible(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'publish.sqlite3'
        self.lock = threading.RLock()
        db = self._db()
        try:
            db.executescript(SCHEMA)
            self._migrate(db)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _migrate(db):
        """Idempotent: databases created before project grouping lack the snapshot columns."""
        have = {row[1] for row in db.execute('PRAGMA table_info(posts)')}
        for name in ('project_id', 'project_name'):
            if name not in have:
                db.execute(f'ALTER TABLE posts ADD COLUMN {name} TEXT')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def _run(self, sql, args=(), write=False):
        with self.lock:
            db = self._db()
            try:
                rows = db.execute(sql, args).fetchall()
                if write:
                    db.commit()
                return rows
            finally:
                db.close()

    def recover(self):
        """Called only while holding the cross-process publishing lock."""
        stamp = time.time()
        for post in self.posts(status='uploading'):
            if post['external_id']:
                self.update_post(post['id'], status='scheduled', next_poll_at=stamp)
            elif post['upload_session'] and post['platform'] == 'youtube':
                self.update_post(post['id'], status='scheduled', error='Reconnecting to the existing YouTube upload.')
            else:
                self.update_post(post['id'], status='failed', error='Upload result is uncertain. Check the platform before trying again.')

    def pause_after_restore(self):
        """Remote effects cannot be rolled back: restored uploads require review."""
        self._run("UPDATE posts SET status='draft', scheduled_at=NULL, error=?, updated_at=? "
                  "WHERE status IN ('scheduled','uploading')",
                  ('Restored from recovery. Check the remote publishing state before scheduling again.', time.time()), True)

    # -- settings -----------------------------------------------------------
    def setting(self, key, default=None):
        rows = self._run('SELECT value FROM settings WHERE key=?', (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_setting(self, key, value):
        if value is None:
            self._run('DELETE FROM settings WHERE key=?', (key,), True)
        else:
            self._run('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key, dump(value)), True)

    # -- channels -----------------------------------------------------------
    @staticmethod
    def _channel(row):
        return {**dict(row), 'scopes': json.loads(row['scopes'])}

    def add_channel(self, platform, provider, display_name, external_id, scopes, token_ref):
        with self.lock:
            existing = self._run('SELECT * FROM channel_accounts WHERE platform=? AND provider=? AND external_id=?',
                                 (platform, provider, external_id))
            if existing:
                self._run('UPDATE channel_accounts SET display_name=?, scopes=?, token_ref=? WHERE id=?',
                          (display_name, dump(scopes), token_ref, existing[0]['id']), True)
                return self.get_channel(existing[0]['id'])
            identity = uuid.uuid4().hex
            self._run('INSERT INTO channel_accounts VALUES (?,?,?,?,?,?,?,?)',
                      (identity, platform, provider, display_name, external_id, dump(scopes), token_ref, time.time()), True)
            return self.get_channel(identity)

    def get_channel(self, channel_id):
        rows = self._run('SELECT * FROM channel_accounts WHERE id=?', (channel_id,))
        if not rows:
            raise KeyError(channel_id)
        return self._channel(rows[0])

    def channels(self):
        return [self._channel(r) for r in self._run('SELECT * FROM channel_accounts ORDER BY created_at')]

    def delete_channel(self, channel_id):
        self.get_channel(channel_id)
        self._run('DELETE FROM channel_accounts WHERE id=?', (channel_id,), True)

    # -- posts --------------------------------------------------------------
    @staticmethod
    def _post(row):
        post = dict(row)
        for name in POST_JSON:
            post[name] = json.loads(post[name])
        return post

    def create_post(self, **fields):
        stamp = time.time()
        post = {'id': uuid.uuid4().hex, 'artifact_id': None, 'render_job_id': None, 'description': '', 'hashtags': [],
                'scheduled_at': None, 'published_at': None, 'external_id': None, 'url': None, 'error': None,
                'warnings': [], 'duration': None, 'upload_session': None, 'attempts': 0, 'next_attempt_at': None,
                'poll_stage': 0, 'next_poll_at': None, 'project_id': None, 'project_name': None, 'created_at': stamp, 'updated_at': stamp, **fields}
        values = [dump(post[c]) if c in POST_JSON else post[c] for c in POST_COLUMNS]
        self._run(f'INSERT INTO posts ({", ".join(POST_COLUMNS)}) VALUES ({", ".join("?" * len(POST_COLUMNS))})',
                  values, True)
        return self.get_post(post['id'])

    def get_post(self, post_id):
        rows = self._run('SELECT * FROM posts WHERE id=?', (post_id,))
        if not rows:
            raise KeyError(post_id)
        return self._post(rows[0])

    def update_post(self, post_id, **fields):
        if not fields:
            return self.get_post(post_id)
        fields['updated_at'] = time.time()
        unknown = set(fields) - set(POST_COLUMNS)
        if unknown:
            raise ValueError(f'unknown post fields: {sorted(unknown)}')
        names = list(fields)
        values = [dump(fields[n]) if n in POST_JSON else fields[n] for n in names]
        with self.lock:
            conn = self._db()
            try:
                conn.execute('BEGIN IMMEDIATE')
                before = conn.execute('SELECT status, attempts FROM posts WHERE id=?', (post_id,)).fetchone()
                if before is None:
                    raise KeyError(post_id)
                conn.execute(f'UPDATE posts SET {", ".join(n + "=?" for n in names)} WHERE id=?', values + [post_id])
                if 'status' in fields and fields['status'] != before['status']:
                    conn.execute('INSERT INTO post_events(post_id,at,status,attempt) VALUES (?,?,?,?)',
                                 (post_id, fields['updated_at'], fields['status'], fields.get('attempts', before['attempts'])))
                conn.commit()
            finally:
                conn.close()
        return self.get_post(post_id)

    def set_project(self, post_id, project_id, project_name):
        """Persist the project snapshot without touching updated_at or status events."""
        self._run('UPDATE posts SET project_id=?, project_name=? WHERE id=?', (project_id, project_name, post_id), True)

    def snapshot_project_names(self, collection_id, name, short_ids=()):
        """Freeze the project label on every post of a project (call before a purge deletes its shorts).

        Matches posts already tied to `collection_id` plus any post whose short is in `short_ids`
        (covers posts not yet lazily backfilled). Returns the number of posts updated."""
        short_ids = list(short_ids)
        marks = ','.join('?' * len(short_ids))
        where = 'project_id=?' + (f' OR short_id IN ({marks})' if short_ids else '')
        with self.lock:
            db = self._db()
            try:
                cursor = db.execute(f'UPDATE posts SET project_id=?, project_name=? WHERE {where}',
                                    [collection_id, name, collection_id, *short_ids])
                db.commit()
                return cursor.rowcount
            finally:
                db.close()

    def post_events(self, post_id):
        return [{'at': row['at'], 'status': row['status'], 'message': {
            'draft': 'Draft', 'scheduled': 'Waiting or checking', 'uploading': 'Uploading',
            'published': 'Published', 'failed': 'Needs attention'}.get(row['status'], 'Updated'),
                 'attempt': row['attempt']} for row in self._run(
                     'SELECT at,status,attempt FROM post_events WHERE post_id=? ORDER BY id DESC LIMIT 20', (post_id,))]

    def posts(self, short_id=None, status=None, revision_id=None):
        clauses, values = [], []
        for name, value in (('short_id', short_id), ('status', status), ('revision_id', revision_id)):
            if value:
                clauses.append(f'{name}=?')
                values.append(value)
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        return [self._post(r) for r in self._run(f'SELECT * FROM posts {where} ORDER BY created_at, rowid', values)]

    def delete_post(self, post_id):
        self.get_post(post_id)
        for table in ('metric_snapshots', 'retention_curves', 'post_events'):
            self._run(f'DELETE FROM {table} WHERE post_id=?', (post_id,), True)
        self._run('DELETE FROM posts WHERE id=?', (post_id,), True)

    def awaiting_render(self):
        return [self._post(r) for r in self._run(
            "SELECT * FROM posts WHERE artifact_id IS NULL AND render_job_id IS NOT NULL AND status IN ('draft','scheduled')")]

    def due_uploads(self, now):
        rows = self._run("SELECT * FROM posts WHERE status='scheduled' AND external_id IS NULL AND artifact_id IS NOT NULL "
                         "AND COALESCE(next_attempt_at, 0) <= ? ORDER BY COALESCE(scheduled_at, 0), created_at", (now,))
        return [self._post(r) for r in rows]

    def due_polls(self, now):
        rows = self._run("SELECT * FROM posts WHERE status IN ('scheduled','published') AND external_id IS NOT NULL "
                         "AND next_poll_at IS NOT NULL AND next_poll_at <= ? ORDER BY next_poll_at", (now,))
        return [self._post(r) for r in rows]

    def next_wake(self):
        """Earliest time any post needs attention, or None."""
        rows = self._run("SELECT MIN(t) FROM (SELECT COALESCE(next_attempt_at, 0) AS t FROM posts WHERE status='scheduled' "
                         "AND external_id IS NULL AND artifact_id IS NOT NULL UNION ALL SELECT next_poll_at FROM posts "
                         "WHERE status IN ('scheduled','published') AND external_id IS NOT NULL AND next_poll_at IS NOT NULL)")
        return rows[0][0] if rows else None

    # -- metrics ------------------------------------------------------------
    def add_snapshot(self, post_id, metrics, fetched_at=None, source='local'):
        self._run('INSERT INTO metric_snapshots (post_id, fetched_at, source, metrics) VALUES (?,?,?,?)',
                  (post_id, fetched_at or time.time(), source, dump(metrics)), True)

    def snapshots(self, post_id):
        return [{'post_id': r['post_id'], 'fetched_at': r['fetched_at'], 'source': r['source'], 'metrics': json.loads(r['metrics'])}
                for r in self._run('SELECT * FROM metric_snapshots WHERE post_id=? ORDER BY fetched_at, id', (post_id,))]

    def latest_snapshot(self, post_id):
        items = self.snapshots(post_id)
        return items[-1] if items else None

    def has_snapshot(self, post_id, fetched_at, source):
        return bool(self._run('SELECT 1 FROM metric_snapshots WHERE post_id=? AND fetched_at=? AND source=?',
                              (post_id, fetched_at, source)))

    def add_retention(self, post_id, points, fetched_at=None):
        self._run('INSERT INTO retention_curves (post_id, fetched_at, points) VALUES (?,?,?)',
                  (post_id, fetched_at or time.time(), dump(points)), True)

    def latest_retention(self, post_id):
        rows = self._run('SELECT * FROM retention_curves WHERE post_id=? ORDER BY fetched_at DESC, id DESC LIMIT 1', (post_id,))
        if not rows:
            return None
        return {'post_id': post_id, 'fetched_at': rows[0]['fetched_at'], 'points': json.loads(rows[0]['points'])}

    # -- oauth --------------------------------------------------------------
    def put_state(self, state, platform, verifier, redirect_uri):
        self._run('DELETE FROM oauth_states WHERE created_at < ?', (time.time() - 900,), True)
        self._run('INSERT INTO oauth_states VALUES (?,?,?,?,?)', (state, platform, verifier, redirect_uri, time.time()), True)

    def pop_state(self, state):
        rows = self._run('SELECT * FROM oauth_states WHERE state=? AND created_at >= ?', (state, time.time() - 900))
        self._run('DELETE FROM oauth_states WHERE state=?', (state,), True)
        return dict(rows[0]) if rows else None


def snapshot_project_names(collection_id, name, short_ids=(), store=None):
    """Module-level entry for the lifecycle purge. Uses the app's publish store unless `store` is given."""
    if store is None:
        from .. import studio_api
        from . import runtime
        store = runtime.store(studio_api.data_root())
    return store.snapshot_project_names(collection_id, name, short_ids)
