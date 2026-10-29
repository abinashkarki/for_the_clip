"""SQLite state: devices (token hashes), encrypted accounts, tracked posts, metric snapshots."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid

SCHEMA = '''
CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (device_id TEXT NOT NULL, channel_id TEXT NOT NULL, platform TEXT NOT NULL,
  sealed TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (device_id, channel_id));
CREATE TABLE IF NOT EXISTS posts (device_id TEXT NOT NULL, post_id TEXT NOT NULL, channel_id TEXT NOT NULL, external_id TEXT NOT NULL,
  published_at REAL, scheduled_at REAL, created_at REAL NOT NULL, stage INTEGER NOT NULL DEFAULT 0, next_poll_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, PRIMARY KEY (device_id, post_id));
CREATE TABLE IF NOT EXISTS snapshots (seq INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, post_id TEXT NOT NULL,
  fetched_at REAL NOT NULL, stored_at REAL NOT NULL, metrics TEXT NOT NULL, retention TEXT);
CREATE INDEX IF NOT EXISTS snapshots_device ON snapshots (device_id, stored_at);
'''


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path, self.lock = str(path), threading.RLock()
        db = self.db()
        try:
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()

    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def run(self, sql, args=(), write=False):
        with self.lock:
            db = self.db()
            try:
                rows = db.execute(sql, args).fetchall()
                if write:
                    db.commit()
                return rows
            finally:
                db.close()

    # -- devices
    def create_device(self, name):
        token = secrets.token_urlsafe(32)
        identity = uuid.uuid4().hex
        self.run('INSERT INTO devices VALUES (?,?,?,?)', (identity, name, hash_token(token), time.time()), True)
        return identity, token

    def device_for(self, token):
        digest = hash_token(token)
        for row in self.run('SELECT id, token_hash FROM devices WHERE token_hash=?', (digest,)):
            if hmac.compare_digest(row['token_hash'], digest):
                return row['id']
        return None

    # -- accounts and posts
    def put_account(self, device, channel, platform, sealed):
        self.run('INSERT INTO accounts VALUES (?,?,?,?,?) ON CONFLICT(device_id, channel_id) DO UPDATE SET '
                 'platform=excluded.platform, sealed=excluded.sealed, updated_at=excluded.updated_at',
                 (device, channel, platform, sealed, time.time()), True)

    def get_account(self, device, channel):
        rows = self.run('SELECT * FROM accounts WHERE device_id=? AND channel_id=?', (device, channel))
        return dict(rows[0]) if rows else None

    def delete_account(self, device, channel):
        self.run('DELETE FROM accounts WHERE device_id=? AND channel_id=?', (device, channel), True)
        self.run('DELETE FROM posts WHERE device_id=? AND channel_id=?', (device, channel), True)

    def put_post(self, device, post_id, channel, external_id, published_at, scheduled_at, created_at, next_poll_at):
        existing = self.run('SELECT 1 FROM posts WHERE device_id=? AND post_id=?', (device, post_id))
        if existing:
            self.run('UPDATE posts SET channel_id=?, external_id=?, published_at=COALESCE(?, published_at), scheduled_at=? '
                     'WHERE device_id=? AND post_id=?', (channel, external_id, published_at, scheduled_at, device, post_id), True)
        else:
            self.run('INSERT INTO posts (device_id, post_id, channel_id, external_id, published_at, scheduled_at, created_at, '
                     'next_poll_at) VALUES (?,?,?,?,?,?,?,?)',
                     (device, post_id, channel, external_id, published_at, scheduled_at, created_at, next_poll_at), True)

    def delete_post(self, device, post_id):
        self.run('DELETE FROM posts WHERE device_id=? AND post_id=?', (device, post_id), True)
        self.run('DELETE FROM snapshots WHERE device_id=? AND post_id=?', (device, post_id), True)

    def get_post(self, device, post_id):
        rows = self.run('SELECT * FROM posts WHERE device_id=? AND post_id=?', (device, post_id))
        return dict(rows[0]) if rows else None

    def due_posts(self, now):
        return [dict(r) for r in self.run('SELECT * FROM posts WHERE next_poll_at IS NOT NULL AND next_poll_at <= ? '
                                          'ORDER BY next_poll_at', (now,))]

    def update_post(self, device, post_id, **fields):
        names = list(fields)
        self.run(f'UPDATE posts SET {", ".join(n + "=?" for n in names)} WHERE device_id=? AND post_id=?',
                 [fields[n] for n in names] + [device, post_id], True)

    # -- snapshots
    def add_snapshot(self, device, post_id, fetched_at, metrics, retention):
        self.run('INSERT INTO snapshots (device_id, post_id, fetched_at, stored_at, metrics, retention) VALUES (?,?,?,?,?,?)',
                 (device, post_id, fetched_at, self.next_stored(device), json.dumps(metrics),
                  json.dumps(retention) if retention else None), True)

    def next_stored(self, device):
        """Monotonic per-device cursor so `since=` never skips a snapshot stored in the same instant."""
        rows = self.run('SELECT MAX(stored_at) FROM snapshots WHERE device_id=?', (device,))
        last = rows[0][0] or 0.0
        return max(time.time(), last + 0.001)

    def snapshots_since(self, device, since, limit=500):
        rows = self.run('SELECT * FROM snapshots WHERE device_id=? AND stored_at > ? ORDER BY stored_at LIMIT ?',
                        (device, since, limit))
        return [{'post_id': r['post_id'], 'fetched_at': r['fetched_at'], 'stored_at': r['stored_at'],
                 'metrics': json.loads(r['metrics']), 'retention': json.loads(r['retention']) if r['retention'] else None}
                for r in rows]
