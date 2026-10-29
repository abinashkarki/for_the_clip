"""Additive collection state. Transactions serialize dispatch, cancellation and promotion.

Reservations are charged before dispatch, including interrupted attempts. New invocation
ledgers settle terminal known usage independently; unknown and legacy usage remain
conservative. Proven no-request cache reuse releases its reservation.
No provider or filesystem artifact work is performed here.
"""
import json
import math
import sqlite3
import unicodedata
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


def export_review_state(revision, decisions=()):
    """Eligibility for one immutable revision; human decisions never replace machine checks."""
    document = revision['document']
    review = document.get('editorial', {})
    issues = review.get('issues') or []
    machine_pass = (not document.get('inspection_only') and document.get('technical') == 'pass' and review.get('status') == 'pass'
                    and not any(issue.get('severity') == 'critical' for issue in issues))
    exact = [decision for decision in decisions if decision.get('revision_id') == revision['id']]
    latest = exact[-1] if exact else {}
    acknowledged = bool(machine_pass and issues and latest.get('rating') == 'keep'
                        and latest.get('acknowledge_warnings') is True)
    return {'export_review_required': bool(issues),
            'export_review_acknowledged': acknowledged,
            'export_eligible': bool(machine_pass and (not issues or acknowledged))}


def require_export_eligible(revision, decisions=()):
    state = export_review_state(revision, decisions)
    if not state['export_eligible']:
        raise ValueError('This revision requires passing technical/editorial checks and explicit warning acknowledgment before export.')
    return state


class CollectionRequestConflict(ValueError):
    """A collection creation key was reused for different inputs."""


class CollectionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        from .storage_safety import assert_compatible
        assert_compatible(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'collections.sqlite3'
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, parent TEXT, document TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS records_kind_parent ON records(kind,parent)')
            db.execute('CREATE TABLE IF NOT EXISTS collection_requests (request_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS enqueue_keys (collection_id TEXT, key TEXT, task_id TEXT, PRIMARY KEY(collection_id,key))')

    @contextmanager
    def _transaction(self, *, write=True):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _get(self, db, identity, kind=None):
        row = db.execute('SELECT kind,document FROM records WHERE id=?', (identity,)).fetchone()
        if row is None or (kind and row[0] != kind):
            raise KeyError(identity)
        return json.loads(row[1])

    def _save(self, db, kind, doc, parent=None):
        db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document',
                   (doc['id'], kind, parent, json.dumps(doc, allow_nan=False)))
        return json.loads(json.dumps(doc, allow_nan=False))

    def _list(self, db, kind, parent=None):
        query = 'SELECT document FROM records WHERE kind=?'
        args = [kind]
        if parent is not None:
            query += ' AND parent=?'
            args.append(parent)
        return [json.loads(r[0]) for r in db.execute(query + ' ORDER BY rowid', args)]

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Allowance must be a finite nonnegative number')
        return value

    @staticmethod
    def _count(value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('Allowance must be a nonnegative integer')
        return value

    @staticmethod
    def _creation_input(source_ids, constraints=None, budget=None, name='', request_id=None):
        if not isinstance(source_ids, list) or not source_ids or any(not isinstance(s, str) or not s for s in source_ids):
            raise ValueError('source_ids must be a nonempty list of IDs')
        if len(set(source_ids)) != len(source_ids):
            raise ValueError('Duplicate source IDs')
        if not isinstance(name, str) or len(name) > 100 or any(unicodedata.category(c) == 'Cc' for c in name):
            raise ValueError('Name must be text of at most 100 characters without control characters')
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 100):
            raise ValueError('Request ID must be nonempty text of at most 100 characters')
        budget = budget or {'tokens': 0, 'calls': 0}
        limits = {k: CollectionStore._count(budget.get(k, 0)) for k in ('tokens', 'calls')}
        return json.loads(json.dumps(dict(source_ids=source_ids, constraints=constraints or {}, budget=limits,
                                         name=name.strip()), allow_nan=False))

    def _creation_replay(self, db, request_id, requested):
        if request_id is None:
            return None
        row = db.execute('SELECT collection_id,document FROM collection_requests WHERE request_id=?',
                         (request_id,)).fetchone()
        if row is None:
            return None
        if json.loads(row[1]) != requested:
            raise CollectionRequestConflict('Collection request ID reused with different inputs')
        return self._get(db, row[0], 'collection')

    def find_collection_request(self, source_ids, constraints=None, budget=None, name='', request_id=None):
        """Resolve immutable creation inputs without consulting or changing source state."""
        requested = self._creation_input(source_ids, constraints, budget, name, request_id)
        with self._transaction(write=False) as db:
            return self._creation_replay(db, request_id, requested)

    def create_collection(self, source_ids, constraints=None, budget=None, name='', request_id=None):
        requested = self._creation_input(source_ids, constraints, budget, name, request_id)
        with self._transaction() as db:
            existing = self._creation_replay(db, request_id, requested)
            if existing is not None:
                return existing
            doc = dict(id=uuid.uuid4().hex, **requested, usage={'tokens': 0, 'calls': 0},
                       status='open', created_at=time.time())
            saved = self._save(db, 'collection', doc)
            if request_id is not None:
                db.execute('INSERT INTO collection_requests VALUES (?,?,?)',
                           (request_id, doc['id'], json.dumps(requested, allow_nan=False)))
            return saved

    def get_collection(self, collection_id):
        with self._transaction(write=False) as db:
            return self._get(db, collection_id, 'collection')

    def list_collections(self):
        with self._transaction(write=False) as db:
            return self._list(db, 'collection')

    def usage_report(self, collection_id):
        """Read incurred paid attempts in one snapshot, without reconciling charges.

        This diagnostic snapshot may advance independently of other detail reads.
        Historical attempts remain authoritative after cancellation or run replacement.
        """
        report = dict(attempts=0, reported_calls=0, reported_tokens=0,
                      unknown_call_attempts=0, unknown_token_attempts=0,
                      active_attempts=0,
                      cost=dict(status='unknown', amount=None, currency=None))
        with closing(sqlite3.connect(f'{self.path.as_uri()}?mode=ro', uri=True, timeout=30)) as db:
            db.execute('BEGIN')
            self._get(db, collection_id, 'collection')
            for (document,) in db.execute("SELECT document FROM records WHERE kind='attempt'"):
                attempt = json.loads(document)
                if attempt['collection_id'] != collection_id or not any(
                        attempt['reservation'][key] > 0 for key in ('calls', 'tokens')):
                    continue
                report['attempts'] += 1
                report['active_attempts'] += int(attempt['status'] == 'active')
                for key, singular in (('calls', 'call'), ('tokens', 'token')):
                    value = attempt['reported_usage'][key]
                    if value is None:
                        report[f'unknown_{singular}_attempts'] += 1
                    else:
                        report[f'reported_{key}'] += value
        return report

    def start_run(self, collection_id, configuration, request_id):
        if not isinstance(configuration, dict) or not isinstance(request_id, str) or not request_id:
            raise ValueError('Run requires configuration and request ID')
        # Validate and detach caller-owned nested values before persisting the snapshot.
        configuration = json.loads(json.dumps(configuration, allow_nan=False))
        with self._transaction() as db:
            collection = self._active(db, collection_id)
            for run in self._list(db, 'run', collection_id):
                if run['request_id'] == request_id:
                    if run['configuration'] != configuration:
                        raise ValueError('Run request ID reused with different configuration')
                    return run
            if collection.get('current_run'):
                current = self._get(db, collection['current_run'], 'run')
                if current['status'] == 'active':
                    raise ValueError('Collection already has an active run')
            run = dict(id=uuid.uuid4().hex, collection_id=collection_id, configuration=configuration,
                       request_id=request_id, status='active', created_at=time.time())
            self._save(db, 'run', run, collection_id)
            collection['current_run'] = run['id']
            self._save(db, 'collection', collection)
            return run

    def continue_run(self, run_id, tokens, calls, seconds, request_id):
        increments = dict(tokens=self._count(tokens), calls=self._count(calls))
        self._count(seconds)
        if not 30 <= seconds <= 900 or not isinstance(request_id, str) or not request_id:
            raise ValueError('Continuation requires 30..900 seconds and a request ID')
        requested = dict(increments, seconds=seconds)
        with self._transaction() as db:
            run = self._get(db, run_id, 'run')
            collection = self._active(db, run['collection_id'])
            if collection.get('current_run') != run_id or run['status'] == 'cancelled':
                raise ValueError('Run is no longer current or is cancelled')
            history = run.setdefault('continuations', {})
            if request_id in history:
                if history[request_id]['request'] != requested:
                    raise ValueError('Continuation request ID reused with different inputs')
                return run
            eligible = [task for task in self._list(db, 'task', collection['id'])
                        if task.get('payload', {}).get('run_id') == run_id
                        and task['attempts'] < task['max_attempts'] and
                        (task['status'] in ('held_budget', 'held_dependency') or
                         (task['status'] == 'failed' and task.get('error') == 'provider_deadline'))]
            if not eligible:
                raise ValueError('Run has no remaining continuable tasks')
            deadline = time.time() + seconds
            history[request_id] = dict(request=requested, deadline=deadline)
            for key, value in increments.items():
                collection['budget'][key] += value
            for task in eligible:
                if task['status'] == 'held_budget' and any(
                        task['reservation'][k] > 0 and collection['usage'][k] + task['reservation'][k] > collection['budget'][k]
                        for k in ('tokens', 'calls')):
                    continue
                task.update(status='queued', error=None)
                self._save(db, 'task', task)
            run.update(status='active', deadline=deadline)
            run.pop('finished_at', None)
            self._save(db, 'collection', collection)
            return self._save(db, 'run', run)

    def extend_run(self, run_id, *, tokens, calls, seconds):
        """User-initiated Resume: renew the run deadline and add allowance, whatever its tasks' state."""
        tokens, calls = self._count(tokens), self._count(calls)
        if not 30 <= seconds <= 3600:
            raise ValueError('Extension requires 30..3600 seconds')
        with self._transaction() as db:
            run = self._get(db, run_id, 'run')
            collection = self._active(db, run['collection_id'])
            if collection.get('current_run') != run_id or run['status'] == 'cancelled':
                raise ValueError('Run is no longer current or is cancelled')
            collection['budget']['tokens'] += tokens
            collection['budget']['calls'] += calls
            run.update(status='active', deadline=time.time() + seconds)
            run.pop('finished_at', None)
            self._save(db, 'collection', collection)
            return self._save(db, 'run', run)

    def start_execution_clock(self, run_id):
        """Start the active run allowance once actual execution leaves the queue.

        Persisted once, so restart/duplicate dispatch cannot renew paid work.
        Explicit continuations may extend the deadline separately.
        """
        with self._transaction() as db:
            run = self._get(db, run_id, 'run')
            seconds = run['configuration'].get('seconds')
            if run.get('execution_started_at') is None and isinstance(seconds, (int, float)):
                run['execution_started_at'] = time.time()
                run['deadline'] = max(run.get('deadline', 0), run['execution_started_at'] + seconds)
                self._save(db, 'run', run)
            return run

    def get_run(self, run_id):
        with self._transaction(write=False) as db:
            return self._get(db, run_id, 'run')

    def finish_run(self, run_id, status):
        if status not in ('completed', 'needs_attention', 'cancelled'):
            raise ValueError('Invalid terminal run status')
        with self._transaction() as db:
            run = self._get(db, run_id, 'run')
            collection = self._get(db, run['collection_id'], 'collection')
            if collection.get('current_run') != run_id:
                raise ValueError('Run is no longer current')
            if run['status'] != 'active':
                if run['status'] == status:
                    return run
                raise ValueError('Run is already terminal')
            if collection['status'] == 'cancelled' and status != 'cancelled':
                raise ValueError('Collection cancelled')
            run.update(status=status, finished_at=time.time())
            return self._save(db, 'run', run)

    def get_task(self, task_id):
        with self._transaction(write=False) as db:
            return self._get(db, task_id, 'task')

    def _reactivate_run(self, db, task):
        run_id = task.get('payload', {}).get('run_id')
        if not run_id:
            return
        collection = self._get(db, task['collection_id'], 'collection')
        if collection.get('current_run') != run_id:
            raise ValueError('Task belongs to a superseded run')
        run = self._get(db, run_id, 'run')
        if run['status'] in {'completed', 'needs_attention'}:
            run['status'] = 'active'
            run.pop('finished_at', None)
            self._save(db, 'run', run)

    def save_candidates(self, collection_id, candidates, input_version, task_id, lease_token, slate=None):
        """Commit one immutable slate per task; retries adopt the original commit."""
        if not isinstance(candidates, list) or any(not isinstance(c, dict) or not isinstance(c.get('id'), str) or not c['id'] for c in candidates):
            raise ValueError('Candidates require stable IDs')
        if len({c['id'] for c in candidates}) != len(candidates):
            raise ValueError('Duplicate candidate IDs')
        with self._transaction() as db:
            task = self._fence(db, task_id, lease_token, input_version, time.time())
            if task['collection_id'] != collection_id:
                raise ValueError('Task belongs to another collection')
            if 'committed_candidates' in task:
                return [self._get(db, identity, 'candidate') for identity in task['committed_candidates']]
            records = []
            for candidate in candidates:
                record = dict(id=uuid.uuid4().hex, candidate_id=candidate['id'], collection_id=collection_id,
                              input_version=input_version, task_id=task_id, document=candidate, created_at=time.time())
                records.append(self._save(db, 'candidate', record, collection_id))
            task['committed_candidates'] = [record['id'] for record in records]
            if slate is not None:
                task['committed_slate'] = slate
            self._save(db, 'task', task)
            return records

    def list_candidates(self, collection_id, input_version=None):
        with self._transaction(write=False) as db:
            self._get(db, collection_id, 'collection')
            return [c for c in self._list(db, 'candidate', collection_id)
                    if input_version is None or c['input_version'] == input_version]

    def get_candidate(self, collection_id, candidate_id, input_version=None):
        candidates = self.list_candidates(collection_id, input_version)
        for candidate in reversed(candidates):
            if candidate['candidate_id'] == candidate_id or candidate['id'] == candidate_id:
                return candidate
        raise KeyError(candidate_id)

    def ensure_short(self, collection_id, candidate_id, metadata=None, *, task_id, lease_token, input_version):
        """Atomically adopt or create a short for a stable candidate ID."""
        with self._transaction() as db:
            task = self._fence(db, task_id, lease_token, input_version, time.time())
            if task['collection_id'] != collection_id:
                raise ValueError('Task belongs to another collection')
            candidates = self._list(db, 'candidate', collection_id)
            candidate = next((c for c in reversed(candidates) if
                              (c['candidate_id'] == candidate_id or c['id'] == candidate_id)
                              and c['input_version'] == input_version), None)
            if candidate is None:
                raise KeyError(candidate_id)
            stable_id = candidate['candidate_id']
            for short in self._list(db, 'short', collection_id):
                if short.get('candidate_id') == stable_id and short.get('input_version') == input_version:
                    return short
            return self._save(db, 'short', dict(id=uuid.uuid4().hex, collection_id=collection_id,
                              candidate_id=stable_id, candidate_record_id=candidate['id'], input_version=input_version,
                              metadata=metadata or {}, current_revision=None), collection_id)

    def extend_budget(self, collection_id, tokens=0, calls=0, *, request_id):
        increments = dict(tokens=self._count(tokens), calls=self._count(calls))
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('Budget extension requires a request ID')
        with self._transaction() as db:
            collection = self._get(db, collection_id, 'collection')
            extensions = collection.setdefault('budget_extensions', {})
            if request_id in extensions:
                if extensions[request_id] != increments:
                    raise ValueError('Request ID reused with different budget increments')
                return collection
            extensions[request_id] = increments
            for key, value in increments.items():
                collection['budget'][key] += value
            self._save(db, 'collection', collection)
            if collection['status'] != 'cancelled':
                for task in self._list(db, 'task', collection_id):
                    if task['status'] == 'held_budget' and all(
                            collection['usage'][k] + task['reservation'][k] <= collection['budget'][k]
                            for k in ('tokens', 'calls')):
                        task.update(status='queued', error=None)
                        self._save(db, 'task', task)
            return collection

    def add_short(self, collection_id, metadata=None):
        with self._transaction() as db:
            self._active(db, collection_id)
            return self._save(db, 'short', dict(id=uuid.uuid4().hex, collection_id=collection_id,
                              metadata=metadata or {}, current_revision=None), collection_id)

    def get_short(self, short_id):
        with self._transaction(write=False) as db:
            return self._get(db, short_id, 'short')

    def list_shorts(self, collection_id):
        with self._transaction(write=False) as db:
            self._get(db, collection_id, 'collection')
            return self._list(db, 'short', collection_id)

    def _active(self, db, collection_id):
        collection = self._get(db, collection_id, 'collection')
        if collection['status'] == 'cancelled':
            raise ValueError('Collection cancelled')
        return collection

    def _fence(self, db, task_id, lease_token, input_version, now):
        task = self._get(db, task_id, 'task')
        collection = self._active(db, task['collection_id'])
        if task.get('payload', {}).get('run_id') and task['payload']['run_id'] != collection.get('current_run'):
            raise ValueError('Stale collection run')
        if task['status'] != 'active' or task['lease_token'] != lease_token or task['input_version'] != input_version or task['lease_until'] <= now:
            raise ValueError('Stale task lease or input version')
        return task

    def append_revision(self, short_id, document, expected_revision=None, *, task_id=None, lease_token=None, input_version=None, promote=True):
        inspection_only = document.get('inspection_only', False)
        if inspection_only and (inspection_only is not True or document.get('technical') != 'pass'
                or document.get('editorial', {}).get('status') not in {'needs_revision', 'uncertain'}):
            raise ValueError('Inspection-only revisions require valid technical checks and a held editorial review')
        with self._transaction() as db:
            short = self._get(db, short_id, 'short')
            self._active(db, short['collection_id'])
            if short['current_revision'] != expected_revision:
                raise ValueError('Current revision changed')
            if task_id is not None:
                task = self._fence(db, task_id, lease_token, input_version, time.time())
                if task.get('committed_revision'):
                    raise ValueError('Task already committed a revision')
                if task['short_id'] != short_id:
                    raise ValueError('Task does not own short')
                if task['payload'].get('more_task') and not inspection_only:
                    from .collection_editor import repeated_idea
                    siblings = [self._get(db, sibling['current_revision'], 'revision')['document']['plan']
                                for sibling in self._list(db, 'short', short['collection_id'])
                                if sibling['id'] != short_id and sibling.get('current_revision')]
                    if repeated_idea(document['plan'], siblings):
                        raise ValueError('No distinct supported idea: current sibling revision duplicates this edit.')

            revision = dict(id=uuid.uuid4().hex, short_id=short_id, document=document,
                            previous_revision=expected_revision, created_at=time.time())
            self._save(db, 'revision', revision, short_id)
            if task_id is not None:
                task['committed_revision'] = revision['id']
                self._save(db, 'task', task)
            if inspection_only:
                short['held_revision'] = revision['id']
                short.pop('held_preview', None)
            elif promote:
                short.pop('held_revision', None)
                short.pop('held_preview', None)
                short['current_revision'] = revision['id']
                short.pop('pending_revision', None)
            else:
                short.pop('held_revision', None)
                short.pop('held_preview', None)
                short['pending_revision'] = revision['id']
            self._save(db, 'short', short)
            return json.loads(json.dumps(revision, allow_nan=False))

    def restore_revision(self, collection_id, short_id, revision_id, expected_revision, request_id):
        """Select saved content atomically; replay returns the original short snapshot."""
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError('Restoration requires a request id')
        with self._transaction() as db:
            short = self._get(db, short_id, 'short')
            if short['collection_id'] != collection_id:
                raise ValueError('Short belongs to another collection')
            inputs = dict(collection_id=collection_id, short_id=short_id,
                          revision_id=revision_id, expected_revision=expected_revision)
            for previous in self._list(db, 'restoration', collection_id):
                if previous['request_id'] == request_id:
                    if previous['inputs'] != inputs:
                        raise ValueError('Restoration request id reused with different inputs')
                    return previous['result']
            self._active(db, collection_id)
            if any((t['stage'] == 'discover_more' or t['payload'].get('more_task'))
                   and t['status'] in {'queued', 'active', 'held_budget', 'held_dependency'}
                   for t in self._list(db, 'task', collection_id)):
                raise ValueError('This collection is still processing.')
            if short['current_revision'] != expected_revision:
                raise ValueError('Current revision changed')
            revision = self._get(db, revision_id, 'revision')
            if revision['short_id'] != short_id:
                raise ValueError('Revision belongs to another short')
            document = revision['document']
            if document.get('inspection_only') or document.get('technical') != 'pass' or document.get('editorial', {}).get('status') != 'pass':
                raise ValueError('Restoration requires passing technical and editorial checks')
            artifacts = [a for a in self._list(db, 'artifact', short_id)
                         if a['revision_id'] == revision_id and a['short_id'] == short_id
                         and a['collection_id'] == collection_id]
            previews = [a for a in artifacts if a['kind'] == 'preview']
            if not previews and document.get('artifact_state') != 'timeline':  # generated timelines carry no preview
                raise ValueError('Restoration requires a saved preview')
            tasks = [t for t in self._list(db, 'task', collection_id) if t['short_id'] == short_id]
            if any(t['status'] in ('queued', 'active') for t in tasks):
                raise ValueError('Short has queued or active tasks')
            for task in tasks:
                if task['status'] in ('failed', 'held_budget', 'held_dependency'):
                    task.update(status='cancelled', lease_token=None, lease_until=None)
                    self._save(db, 'task', task)
            exports = [a for a in artifacts if a['kind'] == 'export']
            restoration_id = uuid.uuid4().hex
            short.update(current_revision=revision_id, current_preview=previews[-1]['id'] if previews else None,
                         current_export=exports[-1]['id'] if exports else None,
                         restored_task_ids=[task['id'] for task in tasks],
                         restoration_id=restoration_id)
            short.pop('pending_revision', None)
            short.pop('held_revision', None)
            short.pop('held_preview', None)
            result = self._save(db, 'short', short)
            self._save(db, 'restoration', dict(id=restoration_id, request_id=request_id,
                       inputs=inputs, result=result, created_at=time.time()), collection_id)
            return result

    def list_restorations(self, collection_id):
        with self._transaction(write=False) as db:
            self._get(db, collection_id, 'collection')
            return self._list(db, 'restoration', collection_id)

    def commit_artifact(self, short_id, revision_id, kind, document, task_id, lease_token, input_version):
        if kind not in ('preview', 'export') or not isinstance(document, dict):
            raise ValueError('Artifact requires preview/export kind and document')
        document = json.loads(json.dumps(document, allow_nan=False))
        if 'revision_id' in document and document['revision_id'] != revision_id:
            raise ValueError('Artifact document revision mismatch')
        # The caller validates artifact paths, hashes and technical checks.
        with self._transaction() as db:
            short = self._get(db, short_id, 'short')
            revision = self._get(db, revision_id, 'revision')
            task = self._fence(db, task_id, lease_token, input_version, time.time())
            if revision['short_id'] != short_id or task['short_id'] != short_id or task['collection_id'] != short['collection_id']:
                raise ValueError('Artifact task, revision and short ownership mismatch')
            if task.get('committed_artifact'):
                existing = self._get(db, task['committed_artifact'], 'artifact')
                if existing['kind'] != kind or existing['revision_id'] != revision_id:
                    raise ValueError('Committed artifact kind or revision mismatch')
                return existing
            if kind == 'export':
                require_export_eligible(revision, self._list(db, 'decision', short_id))
            artifact = dict(id=uuid.uuid4().hex, short_id=short_id, collection_id=short['collection_id'],
                            revision_id=revision_id, kind=kind, document=document,
                            task_id=task_id, input_version=input_version, created_at=time.time())
            self._save(db, 'artifact', artifact, short_id)
            task['committed_artifact'] = artifact['id']
            self._save(db, 'task', task)
            inspection_only = revision['document'].get('inspection_only', False)
            if (kind == 'preview' and inspection_only and short.get('held_revision') == revision_id
                    and short['current_revision'] == revision['previous_revision']):
                short['held_preview'] = artifact['id']
                self._save(db, 'short', short)
            if (kind == 'preview' and not inspection_only and short.get('pending_revision') == revision_id
                    and short['current_revision'] == revision['previous_revision']):
                short['current_revision'] = revision_id
                short.pop('pending_revision', None)
            if not inspection_only and short['current_revision'] == revision_id:
                short['current_' + kind] = artifact['id']
                self._save(db, 'short', short)
            return artifact

    def get_artifact(self, artifact_id):
        with self._transaction(write=False) as db:
            return self._get(db, artifact_id, 'artifact')

    def list_artifacts(self, short_id):
        with self._transaction(write=False) as db:
            self._get(db, short_id, 'short')
            return self._list(db, 'artifact', short_id)

    def save_decision(self, short_id, revision_id, rating, notes='', expected_revision=None, *, acknowledge_warnings=False):
        if rating not in ('keep', 'revise', 'reject') or not isinstance(notes, str) or len(notes) > 8000:
            raise ValueError('Decision requires keep/revise/reject rating and notes up to 8000 characters')
        if type(acknowledge_warnings) is not bool:
            raise ValueError('Warning acknowledgment must be boolean')
        with self._transaction() as db:
            short = self._get(db, short_id, 'short')
            self._active(db, short['collection_id'])
            revision = self._get(db, revision_id, 'revision')
            if revision['short_id'] != short_id:
                raise ValueError('Revision belongs to another short')
            if revision['document'].get('inspection_only'):
                if (short.get('held_revision') != revision_id or expected_revision != revision_id
                        or short['current_revision'] != revision['previous_revision']):
                    raise ValueError('Held decision revision is no longer current')
                if rating == 'keep' or acknowledge_warnings:
                    raise ValueError('Inspection-only held revisions allow only Revise or Reject; export remains blocked')
            elif short['current_revision'] != expected_revision or revision_id != expected_revision:
                raise ValueError('Decision revision is no longer current')
            if acknowledge_warnings:
                proposed = {'revision_id': revision_id, 'rating': rating, 'acknowledge_warnings': True}
                state = export_review_state(revision, [proposed])
                if not state['export_review_acknowledged']:
                    raise ValueError('Only a kept machine-passing revision with noncritical issues can acknowledge warnings')
            decision = dict(id=uuid.uuid4().hex, short_id=short_id, revision_id=revision_id,
                            rating=rating, notes=notes, acknowledge_warnings=acknowledge_warnings, created_at=time.time())
            return self._save(db, 'decision', decision, short_id)

    def export_review_state(self, revision_id, *, require=False):
        with self._transaction() as db:
            revision = self._get(db, revision_id, 'revision')
            decisions = self._list(db, 'decision', revision['short_id'])
            check = require_export_eligible if require else export_review_state
            return check(revision, decisions)

    def get_decision(self, decision_id):
        with self._transaction(write=False) as db:
            return self._get(db, decision_id, 'decision')

    def list_decisions(self, short_id, revision_id=None):
        with self._transaction(write=False) as db:
            self._get(db, short_id, 'short')
            return [d for d in self._list(db, 'decision', short_id)
                    if revision_id is None or d['revision_id'] == revision_id]

    def get_revision(self, revision_id):
        with self._transaction(write=False) as db:
            return self._get(db, revision_id, 'revision')

    def list_revisions(self, short_id):
        with self._transaction(write=False) as db:
            self._get(db, short_id, 'short')
            return self._list(db, 'revision', short_id)

    def enqueue_task(self, collection_id, stage, input_version, idempotency_key, *, short_id=None, dependencies=None, max_attempts=2, reserved_tokens=0, reserved_calls=0, payload=None):
        if not all(isinstance(v, str) and v for v in (stage, input_version, idempotency_key)) or isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError('Task requires stage, input version, key and positive attempt cap')
        reservation = {'tokens': self._count(reserved_tokens), 'calls': self._count(reserved_calls)}
        with self._transaction() as db:
            self._active(db, collection_id)
            existing = db.execute('SELECT task_id FROM enqueue_keys WHERE collection_id=? AND key=?', (collection_id, idempotency_key)).fetchone()
            if existing:
                previous = self._get(db, existing[0], 'task')
                requested = dict(stage=stage, input_version=input_version, short_id=short_id,
                                 dependencies=dependencies or [], max_attempts=max_attempts,
                                 reservation=reservation, payload=payload or {})
                if any(previous[k] != v for k, v in requested.items()):
                    raise ValueError('Idempotency key reused with different task inputs')
                return previous
            unfinished = {'queued', 'active', 'held_budget', 'held_dependency'}
            current_run = self._get(db, collection_id, 'collection').get('current_run')
            all_tasks = [t for t in self._list(db, 'task', collection_id)
                         if not t['payload'].get('run_id') or t['payload']['run_id'] == current_run]
            if stage == 'discover_more':
                graph_pending = any(t['stage'] == 'discover_more' and t['status'] == 'completed'
                    and (t.get('result') or {}).get('candidates')
                    and not any(child['payload'].get('more_task') == t['id'] for child in all_tasks)
                    for t in all_tasks)
                if graph_pending or any(t['status'] in unfinished for t in all_tasks):
                    raise ValueError('This collection is still processing.')
                base = self._get(db, (payload or {})['base_short_id'], 'short')
                if base['current_revision'] != (payload or {})['base_revision']:
                    raise ValueError('The selected revision changed.')
            elif stage in {'plan_short', 'apply_settings'} and not (payload or {}).get('more_task'):
                if any((t['stage'] == 'discover_more' or t['payload'].get('more_task')) and t['status'] in unfinished for t in all_tasks):
                    raise ValueError('This collection is still processing.')
            if short_id and self._get(db, short_id, 'short')['collection_id'] != collection_id:
                raise ValueError('Short belongs to another collection')
            if stage == 'apply_settings':
                short = self._get(db, short_id, 'short')
                if short['current_revision'] != (payload or {}).get('base_revision'):
                    raise ValueError('The selected revision changed.')
                if any(t['short_id'] == short_id and t['status'] in {'active', 'queued'}
                       for t in self._list(db, 'task', collection_id)):
                    raise ValueError('This short is still processing.')
            for dep in dependencies or []:
                if self._get(db, dep, 'task')['collection_id'] != collection_id:
                    raise ValueError('Dependency belongs to another collection')
            task = dict(id=uuid.uuid4().hex, collection_id=collection_id, short_id=short_id, stage=stage,
                        input_version=input_version, idempotency_key=idempotency_key, dependencies=dependencies or [],
                        max_attempts=max_attempts, attempts=0, reservation=reservation, payload=payload or {},
                        status='queued', lease_token=None, lease_until=None, result=None, error=None)
            self._reactivate_run(db, task)
            self._save(db, 'task', task, collection_id)
            db.execute('INSERT INTO enqueue_keys VALUES (?,?,?)', (collection_id, idempotency_key, task['id']))
            return json.loads(json.dumps(task, allow_nan=False))

    def list_tasks(self, collection_id):
        with self._transaction(write=False) as db:
            self._get(db, collection_id, 'collection')
            return self._list(db, 'task', collection_id)

    def _recover(self, db, now):
        recovered = []
        for task in self._list(db, 'task'):
            if task['status'] == 'active' and task['lease_until'] <= now:
                attempt = self._attempt(db, task['id'], task['lease_token'])
                attempt['status'] = 'expired'
                self._save(db, 'attempt', attempt)
                task.update(status='failed' if task['attempts'] >= task['max_attempts'] else 'queued', lease_token=None, lease_until=None, error='lease_expired')
                self._save(db, 'task', task)
                recovered.append(task)
        return recovered

    def recover_expired(self, *, now=None):
        with self._transaction() as db:
            return self._recover(db, time.time() if now is None else now)

    def pause_after_restore(self):
        """Restored work needs explicit Resume; retain conservative usage charges."""
        with self._transaction() as db:
            count = 0
            for task in self._list(db, 'task'):
                if task['status'] not in {'queued', 'active'}:
                    continue
                if task['status'] == 'active' and task.get('lease_token'):
                    attempt = self._attempt(db, task['id'], task['lease_token'])
                    attempt['status'] = 'expired'
                    self._save(db, 'attempt', attempt)
                task.update(status='failed', lease_token=None, lease_until=None,
                            error='restored_backup_review',
                            error_detail='Restored from a recovery copy. Review and Resume; remote usage after the backup is not reverted.')
                self._save(db, 'task', task)
                count += 1
            for run in self._list(db, 'run'):
                if run['status'] == 'active':
                    run.update(status='needs_attention', finished_at=time.time())
                    self._save(db, 'run', run)
            for batch in self._list(db, 'collection_batch'):
                if batch['status'] == 'started':
                    batch['status'] = 'draft'
                    self._save(db, 'collection_batch', batch)
            return count

    @staticmethod
    def _budget_block(collection, required):
        remaining = {key: max(0, collection['budget'][key] - collection['usage'][key])
                     for key in ('tokens', 'calls')}
        dimensions = [key for key in ('tokens', 'calls') if required[key] > remaining[key]]
        return {'dimensions': dimensions, 'required': dict(required), 'remaining': remaining}

    def claim_task(self, *, collection_id=None, lease_seconds=60, now=None, stages=None):
        self._number(lease_seconds)
        if lease_seconds == 0:
            raise ValueError('Lease must be positive')
        now = time.time() if now is None else now
        with self._transaction() as db:
            if collection_id is not None:
                self._get(db, collection_id, 'collection')
            self._recover(db, now)
            for task in self._list(db, 'task', collection_id):
                if task['status'] != 'queued' or (stages is not None and task['stage'] not in stages):
                    continue
                collection = self._active(db, task['collection_id'])
                if task.get('payload', {}).get('run_id') and task['payload']['run_id'] != collection.get('current_run'):
                    task.update(status='cancelled', error='run_superseded')
                    self._save(db, 'task', task)
                    continue
                statuses = [self._get(db, dep, 'task')['status'] for dep in task['dependencies']]
                if any(state in ('failed', 'cancelled', 'held_budget', 'held_dependency') for state in statuses):
                    task.update(status='held_dependency', error='dependency_failed')
                    self._save(db, 'task', task)
                    continue
                if any(state != 'completed' for state in statuses):
                    continue
                if any(task['reservation'][k] > 0 and collection['usage'][k] + task['reservation'][k] > collection['budget'][k] for k in ('tokens', 'calls')):
                    task.update(status='held_budget', error='budget_exhausted',
                                budget_block=self._budget_block(collection, task['reservation']))
                    self._save(db, 'task', task)
                    continue
                for k in ('tokens', 'calls'):
                    collection['usage'][k] += task['reservation'][k]
                self._save(db, 'collection', collection)
                task.update(status='active', attempts=task['attempts'] + 1, lease_token=uuid.uuid4().hex, lease_until=now + lease_seconds, error=None)
                task.pop('budget_block', None)
                attempt = dict(id=uuid.uuid4().hex, task_id=task['id'],
                               collection_id=task['collection_id'], lease_token=task['lease_token'],
                               number=task['attempts'], reservation=dict(task['reservation']),
                               charged_usage=dict(task['reservation']), reported_usage={'tokens': None, 'calls': None},
                               status='active', invocation_accounting_version=2)
                self._save(db, 'attempt', attempt, task['id'])
                return self._save(db, 'task', task)
        return None

    def check_lease(self, task_id, lease_token, input_version):
        """Cancellation/ownership polling does not acquire the SQLite writer."""
        with self._transaction(write=False) as db:
            self._fence(db, task_id, lease_token, input_version, time.time())

    def renew_lease(self, task_id, lease_token, input_version, lease_seconds=60, *, now=None):
        self._number(lease_seconds)
        if lease_seconds == 0:
            raise ValueError('Lease must be positive')
        now = time.time() if now is None else now
        with self._transaction() as db:
            task = self._fence(db, task_id, lease_token, input_version, now)
            task['lease_until'] = max(task['lease_until'], now + lease_seconds)
            return self._save(db, 'task', task)

    def list_attempts(self, task_id):
        with self._transaction(write=False) as db:
            self._get(db, task_id, 'task')
            return self._list(db, 'attempt', task_id)

    def _attempt(self, db, task_id, lease_token):
        self._get(db, task_id, 'task')
        for attempt in self._list(db, 'attempt', task_id):
            if attempt['lease_token'] == lease_token:
                return attempt
        raise ValueError('Unknown attempt token')

    def _record_usage(self, db, task_id, lease_token, actual_tokens, actual_calls):
        attempt = self._attempt(db, task_id, lease_token)
        collection = self._get(db, attempt['collection_id'], 'collection')
        aggregate_report = {'tokens': actual_tokens, 'calls': actual_calls}
        if attempt.get('invocations'):
            entries = list(attempt['invocations'].values())
            calls = [entry['calls'] for entry in entries]
            actual_calls = sum(calls) if all(value is not None for value in calls) else None
            if actual_calls is None:
                attempt['reported_usage']['calls'] = None
            known = [entry['tokens'] for entry in entries]
            actual_tokens = sum(known) if all(v is not None for v in known) else None
            if actual_tokens is None:
                attempt['reported_usage']['tokens'] = None
        if (actual_tokens or 0) > 0 or (actual_calls or 0) > 0:
            attempt.pop('no_requests', None)
        for key, actual in (('tokens', actual_tokens), ('calls', actual_calls)):
            if actual is not None:
                self._count(actual)
                known = attempt['reported_usage'][key]
                attempt['reported_usage'][key] = max(actual, known or 0)
        modern = (attempt.get('invocation_accounting_version') == 2 and attempt.get('invocations')
                  and all('reservation' in entry for entry in attempt['invocations'].values()))
        if modern:
            # A late aggregate report may establish excess not attributable to a
            # particular invocation. Keep it as a floor; it cannot identify an
            # unknown sibling or release that sibling's envelope.
            floor = attempt.setdefault('aggregate_usage_floor', {'tokens': 0, 'calls': 0})
            for key, actual in aggregate_report.items():
                if actual is not None:
                    self._count(actual)
                    floor[key] = max(floor[key], actual)
        for key in ('tokens', 'calls'):
            if modern:
                # One unknown invocation must not hide settled usage of its siblings.
                # Only after_attempt reconciliation marks an invocation terminal.
                charged = sum((entry[key] if entry.get('terminal') and entry[key] is not None
                               else entry['reservation'][key]) for entry in attempt['invocations'].values())
                charged = max(charged, attempt['aggregate_usage_floor'][key])
            else:
                charged = (0 if attempt.get('no_requests') else
                           max(attempt['reservation'][key], attempt['reported_usage'][key] or 0))
            collection['usage'][key] += charged - attempt['charged_usage'][key]
            attempt['charged_usage'][key] = charged
        self._save(db, 'collection', collection)
        return self._save(db, 'attempt', attempt)

    def reconcile_no_request_attempts(self, collection_id):
        """Upgrade legacy, completed known-zero attempts; never infer zero from absence.

        Interrupted, expired, failed and admitted physical requests keep their
        charges. This is safe to repeat when dispatch resumes an existing run.
        """
        refunded = {'tokens': 0, 'calls': 0}
        with self._transaction() as db:
            collection = self._get(db, collection_id, 'collection')
            for task in self._list(db, 'task', collection_id):
                for attempt in self._list(db, 'attempt', task['id']):
                    if (attempt['status'] != 'completed' or attempt.get('invocations') or
                            attempt['reported_usage'] != {'tokens': 0, 'calls': 0}):
                        continue
                    if attempt.get('no_requests') and not any(attempt['charged_usage'].values()):
                        continue
                    for key in refunded:
                        refunded[key] += attempt['charged_usage'][key]
                        attempt['charged_usage'][key] = 0
                    attempt['no_requests'] = True
                    self._save(db, 'attempt', attempt)
            if any(refunded.values()):
                for key in refunded:
                    collection['usage'][key] -= refunded[key]
                self._save(db, 'collection', collection)
        return refunded

    def get_stage_checkpoint(self, task_id, identity):
        with self._transaction(write=False) as db:
            self._get(db, task_id, 'task')
            try:
                task = self._get(db, task_id, 'task')
                checkpoint = self._get(db, 'checkpoint-' + identity, 'stage_checkpoint')
                if checkpoint['task_id'] != task_id or checkpoint['input_version'] != task['input_version']:
                    return None
                return checkpoint['response']
            except KeyError:
                return None

    def save_stage_checkpoint(self, task_id, lease_token, identity, response):
        with self._transaction() as db:
            task = self._get(db, task_id, 'task')
            self._fence(db, task_id, lease_token, task['input_version'], time.time())
            return self._save(db, 'stage_checkpoint', {'id': 'checkpoint-' + identity,
                'task_id': task_id, 'input_version': task['input_version'], 'response': response}, task_id)

    def admit_invocation(self, task_id, lease_token, invocation_id, tokens):
        """Atomically reserve each physical request; retain unknown admitted usage.

        New ledgers assign an envelope to each invocation. Known terminal usage
        can release its own unused envelope without releasing unknown siblings.
        """
        self._count(tokens)
        denied = False
        with self._transaction() as db:
            task = self._get(db, task_id, 'task')
            self._fence(db, task_id, lease_token, task['input_version'], time.time())
            attempt = self._attempt(db, task_id, lease_token)
            ledger = attempt.setdefault('invocations', {})
            if invocation_id in ledger:
                return ledger[invocation_id]
            collection = self._get(db, task['collection_id'], 'collection')
            extra = {'calls': 1 if ledger else max(0, 1 - attempt['reservation']['calls']),
                     'tokens': tokens if ledger else max(0, tokens - attempt['reservation']['tokens'])}
            if any(extra[k] and collection['usage'][k] + extra[k] > collection['budget'][k] for k in extra):
                task['budget_block'] = self._budget_block(collection, extra)
                self._save(db, 'task', task)
                denied = True
            else:
                first_reservation = dict(attempt['reservation'])
                for k in extra:
                    collection['usage'][k] += extra[k]
                    attempt['reservation'][k] += extra[k]
                    attempt['charged_usage'][k] += extra[k]
                entry = {'tokens': None, 'calls': None}
                if attempt.get('invocation_accounting_version') == 2:
                    entry.update(reservation=({'tokens': tokens, 'calls': 1} if ledger else
                                               {k: first_reservation[k] + extra[k] for k in extra}),
                                 terminal=False)
                ledger[invocation_id] = entry
                task.pop('budget_block', None)
                self._save(db, 'task', task)
                self._save(db, 'collection', collection)
                self._save(db, 'attempt', attempt)
        # Persist the diagnostic even though the caller receives an exception.
        if denied:
            raise ValueError('invocation_budget_exhausted')
        return entry

    def reconcile_invocation(self, task_id, lease_token, invocation_id, tokens=None):
        """Idempotent additive usage across windows and every physical retry."""
        with self._transaction() as db:
            attempt = self._attempt(db, task_id, lease_token)
            ledger = attempt.get('invocations', {})
            entry = ledger[invocation_id]
            entry['calls'] = 1
            if attempt.get('invocation_accounting_version') == 2:
                entry['terminal'] = True
            if tokens is not None:
                self._count(tokens)
                entry['tokens'] = max(tokens, entry['tokens'] or 0)
            self._save(db, 'attempt', attempt)
            known = [v['tokens'] for v in ledger.values()]
            return self._record_usage(db, task_id, lease_token,
                sum(known) if all(v is not None for v in known) else None, len(ledger))

    def record_usage(self, task_id, lease_token, actual_tokens=None, actual_calls=None):
        """Reconcile historical usage; never authorizes or promotes a result."""
        with self._transaction() as db:
            return self._record_usage(db, task_id, lease_token, actual_tokens, actual_calls)

    def finish_task(self, task_id, lease_token, input_version, *, result=None, error=None, actual_tokens=None, actual_calls=None,
                    now=None, error_detail=None):
        for value in (actual_tokens, actual_calls):
            if value is not None:
                self._count(value)
        with self._transaction() as db:
            task = self._fence(db, task_id, lease_token, input_version, time.time() if now is None else now)
            attempt = self._record_usage(db, task_id, lease_token, actual_tokens, actual_calls)
            if error is None and actual_calls == 0 and actual_tokens == 0 and not attempt.get('invocations'):
                attempt['no_requests'] = True
                collection = self._get(db, task['collection_id'], 'collection')
                for key in ('tokens', 'calls'):
                    collection['usage'][key] -= attempt['charged_usage'][key]
                    attempt['charged_usage'][key] = 0
                self._save(db, 'collection', collection)
            attempt['status'] = 'failed' if error is not None else 'completed'
            self._save(db, 'attempt', attempt)
            task.update(status='failed' if error is not None else 'completed', result=result, error=error, lease_token=None, lease_until=None,
                        error_detail=str(error_detail)[:300] if error is not None and error_detail else None)
            return self._save(db, 'task', task)

    def retry_task(self, task_id, *, grant=0):
        """Queue a failed or held task again. `grant` adds attempts when an explicit user action (Resume) asks."""
        with self._transaction() as db:
            task = self._get(db, task_id, 'task')
            self._active(db, task['collection_id'])
            if grant and task['status'] == 'failed' and task['attempts'] >= task['max_attempts']:
                task['max_attempts'] = task['attempts'] + grant
            if task['status'] not in ('failed', 'held_budget', 'held_dependency') or task['attempts'] >= task['max_attempts']:
                raise ValueError('Task cannot be retried')
            self._reactivate_run(db, task)
            task.update(status='queued', error=None)
            task.pop('budget_block', None)
            return self._save(db, 'task', task)

    def cancel_collection(self, collection_id):
        with self._transaction() as db:
            collection = self._get(db, collection_id, 'collection')
            collection['status'] = 'cancelled'
            if collection.get('current_run'):
                run = self._get(db, collection['current_run'], 'run')
                if run['status'] == 'active':
                    run.update(status='cancelled', finished_at=time.time())
                    self._save(db, 'run', run)
            self._save(db, 'collection', collection)
            for task in self._list(db, 'task', collection_id):
                if task['status'] not in ('completed', 'cancelled'):
                    if task['status'] == 'active':
                        attempt = self._attempt(db, task['id'], task['lease_token'])
                        attempt['status'] = 'cancelled'
                        self._save(db, 'attempt', attempt)
                    task.update(status='cancelled', lease_token=None, lease_until=None)
                    self._save(db, 'task', task)
            return collection

    def delete_collection(self, collection_id):
        """Permanently remove a collection and every record that belongs to it. Returns the number of records."""
        with self._transaction() as db:
            self._get(db, collection_id, 'collection')
            rows = [(r[0], r[1], json.loads(r[2])) for r in db.execute('SELECT id,parent,document FROM records')]
            shorts = {i for i, _, d in rows if d.get('collection_id') == collection_id and 'candidate_id' in d and 'metadata' in d}
            tasks = {i for i, _, d in rows if d.get('collection_id') == collection_id and 'stage' in d and 'idempotency_key' in d}
            doomed = [i for i, parent, d in rows
                      if i == collection_id or parent == collection_id or parent in shorts or parent in tasks
                      or d.get('collection_id') == collection_id or d.get('short_id') in shorts or d.get('task_id') in tasks]
            for identity in doomed:
                db.execute('DELETE FROM records WHERE id=?', (identity,))
            db.execute('DELETE FROM collection_requests WHERE collection_id=?', (collection_id,))
            db.execute('DELETE FROM enqueue_keys WHERE collection_id=?', (collection_id,))
            return len(doomed)

    def set_collection_fields(self, collection_id, **fields):
        """Set top-level lifecycle keys (for example `trashed_at`); None removes the key."""
        with self._transaction() as db:
            collection = self._get(db, collection_id, 'collection')
            for key, value in fields.items():
                if value is None:
                    collection.pop(key, None)
                else:
                    collection[key] = value
            return self._save(db, 'collection', collection)
