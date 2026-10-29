"""Model call log: SQLite rows plus content-addressed gzip JSON blobs.

Never stores keys or raw image bytes: images appear as sha256, mime and size.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

COLUMNS = ('call_id', 'created_at', 'provider', 'model', 'model_version', 'stage', 'prompt_version', 'input_hash',
           'input_chars', 'image_count', 'status', 'error_category', 'input_tokens', 'output_tokens',
           'thinking_tokens', 'cached_tokens', 'cost_usd', 'latency_ms', 'ttft_ms', 'finish_reason',
           'collection_id', 'task_id', 'short_id', 'request_blob', 'response_blob', 'replay_of', 'http_status',
           'response_headers_ms', 'first_response_ms', 'first_content_ms')
INTEGERS = {'input_chars', 'image_count', 'input_tokens', 'output_tokens', 'thinking_tokens', 'cached_tokens',
            'latency_ms', 'ttft_ms', 'http_status', 'response_headers_ms', 'first_response_ms', 'first_content_ms'}
SCHEMA = 'CREATE TABLE IF NOT EXISTS model_calls (' + ', '.join(
    f'{c} {"INTEGER" if c in INTEGERS else "REAL" if c in {"created_at", "cost_usd"} else "TEXT"}'
    + (' PRIMARY KEY' if c == 'call_id' else '') for c in COLUMNS) + ')'
INDEXES = ('CREATE INDEX IF NOT EXISTS model_calls_created ON model_calls (created_at)',
           'CREATE INDEX IF NOT EXISTS model_calls_stage ON model_calls (stage, provider)')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str).encode()


def image_record(image):
    data = image.get('data') or b''
    return {'mime': image.get('mime'), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
            'label': image.get('label')}


class CallLog:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root/'models.sqlite3'
        self.blobs = self.root/'model-calls'
        self.lock = threading.Lock()
        self.ready = False

    def _connect(self):
        from ..storage_safety import assert_compatible
        assert_compatible(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        if not self.ready:
            db.execute(SCHEMA)
            columns = {row[1] for row in db.execute('PRAGMA table_info(model_calls)')}
            for column in ('response_headers_ms', 'first_response_ms', 'first_content_ms'):
                if column not in columns:
                    try:
                        db.execute(f'ALTER TABLE model_calls ADD COLUMN {column} INTEGER')
                    except sqlite3.OperationalError:
                        # Another log instance may have migrated concurrently.
                        if column not in {row[1] for row in db.execute('PRAGMA table_info(model_calls)')}:
                            raise
            for statement in INDEXES:
                db.execute(statement)
            db.commit()
            self.ready = True
        return db

    def put_blob(self, value):
        """Write canonical JSON gzip under its own sha256; returns the hash."""
        raw = canonical(value)
        digest = hashlib.sha256(raw).hexdigest()
        target = self.blobs/digest[:2]/f'{digest}.json.gz'
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f'.tmp{os.getpid()}{threading.get_ident()}')
            temporary.write_bytes(gzip.compress(raw, mtime=0))
            temporary.replace(target)
        return digest

    def read_blob(self, digest):
        if not digest or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            return None
        try:
            return json.loads(gzip.decompress((self.blobs/digest[:2]/f'{digest}.json.gz').read_bytes()))
        except (OSError, ValueError):
            return None

    def record(self, row, request, response):
        row = {**row, 'created_at': row.get('created_at', time.time())}
        row['request_blob'] = self.put_blob(request)
        row['response_blob'] = self.put_blob(response)
        values = [row.get(c) for c in COLUMNS]
        with self.lock:
            db = self._connect()
            try:
                db.execute(f'INSERT INTO model_calls ({", ".join(COLUMNS)}) VALUES ({", ".join("?"*len(COLUMNS))})', values)
                db.commit()
            finally:
                db.close()

    def calls(self, limit=50, **filters):
        clauses, values = [], []
        for name in ('stage', 'provider', 'model', 'status', 'collection_id', 'task_id', 'short_id', 'error_category'):
            if filters.get(name):
                clauses.append(f'{name} = ?')
                values.append(filters[name])
        where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
        limit = max(1, min(int(limit), 1000))
        with self.lock:
            db = self._connect()
            try:
                rows = db.execute(f'SELECT * FROM model_calls {where} ORDER BY created_at DESC, rowid DESC LIMIT ?',
                                  values+[limit]).fetchall()
            finally:
                db.close()
        return [{c: row[c] for c in COLUMNS if c not in {'request_blob', 'response_blob'}} for row in rows]

    def get(self, call_id, blobs=True):
        with self.lock:
            db = self._connect()
            try:
                row = db.execute('SELECT * FROM model_calls WHERE call_id = ?', (call_id,)).fetchone()
            finally:
                db.close()
        if row is None:
            return None
        result = {c: row[c] for c in COLUMNS}
        if blobs:
            result['request'] = self.read_blob(row['request_blob'])
            result['response'] = self.read_blob(row['response_blob'])
        return result
