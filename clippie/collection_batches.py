"""Durable, bounded groups of independent collection runs; no group provider worker."""
import threading
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from . import collection_api as api, telemetry
from .collection_provider import RESERVATION_TOKENS
from .collection_store import CollectionStore

router = APIRouter(prefix='/api/collection-batches', tags=['collection-batches'])
# Serialize group authorization/dispatch/cancellation in the single app process.
_LOCK = threading.RLock()
_PENDING = set()


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=100)
    source_ids: list[str] = Field(min_length=1, max_length=4)
    constraints: api.Constraints = Field(default_factory=api.Constraints)
    overrides: dict[str, dict] = Field(default_factory=dict)
    calls_per_source: StrictInt = Field(default=16, ge=1, le=24)
    seconds_per_source: StrictInt = Field(default=600, ge=30, le=900)

    @model_validator(mode='after')
    def validate_members(self):
        if not self.request_id.strip() or any(not sid.strip() for sid in self.source_ids):
            raise ValueError('IDs must be nonempty.')
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError('Select each source only once.')
        if set(self.overrides) - set(self.source_ids):
            raise ValueError('Overrides must reference selected sources.')
        for sid in self.source_ids:
            self.child_constraints(sid)
        return self

    def child_constraints(self, sid):
        base = self.constraints.model_dump()
        # Group roles can only repeat each child's required primary role.
        if any(key not in self.source_ids or role != 'primary'
               for key, role in base['source_roles'].items()):
            raise ValueError('Batch sources must be primary sources.')
        override = self.overrides.get(sid, {})
        if 'source_roles' in override and override['source_roles'] != {sid: 'primary'}:
            raise ValueError('A child may only reference its own primary source.')
        base.update(override)
        base['source_roles'] = {sid: 'primary'}
        return api.Constraints.model_validate(base).model_dump()


def _get(storage, batch_id):
    with storage._transaction() as db:
        try:
            return storage._get(db, batch_id, 'collection_batch')
        except KeyError:
            raise HTTPException(404, 'Batch not found.')


def _save(storage, batch):
    with storage._transaction() as db:
        storage._save(db, 'collection_batch', batch)


def _sources(ids):
    from .app import sources_for
    sources = sources_for(ids, require_speech=True)
    if any(source.get('status') != 'ready' or not source.get('words') for source in sources.values()):
        raise HTTPException(422, 'Every batch source must be ready with original speech.')
    return sources


def public_batch(storage, batch):
    members = []
    totals = dict(tokens=0, calls=0)
    usage = dict(tokens=0, calls=0)
    summary = dict(ready=0, needs_attention=0, failed=0, active=0)
    reports = {}
    for member in batch['members']:
        child = storage.get_collection(member['collection_id'])
        if child['id'] not in reports:
            reports[child['id']] = storage.usage_report(child['id'])
        for key in totals:
            totals[key] += child['budget'][key]
            usage[key] += child['usage'][key]
        shorts = [api.public_short(s) for s in storage.list_shorts(child['id'])]
        counts = {key: sum(s['status'] == key for s in shorts) for key in summary}
        for key in summary:
            summary[key] += counts[key]
        run = storage.get_run(child['current_run']) if child.get('current_run') else None
        tasks = [task for task in storage.list_tasks(child['id']) if run and
                 task['payload'].get('run_id') == run['id']]
        status = run['status'] if run else ('start_failed' if member.get('error') and child['status'] != 'cancelled' else 'queued' if batch['status'] == 'started' and child['status'] != 'cancelled' else child['status'])
        if status == 'active' and any(task['status'] == 'held_budget' for task in tasks) and not any(
                task['status'] in {'active', 'queued'} for task in tasks):
            status = 'held_budget'
        members.append({**member, 'status': status,
                        'summary': counts, 'budget': child['budget'], 'usage': child['usage'],
                        'usage_report': reports[child['id']],
                        'editor_url': '/collections?collection=' + child['id']})
    initial = batch['initial_allocated']
    # Child reports are individually atomic diagnostic reads, not a group snapshot.
    usage_report = {key: sum(report[key] for report in reports.values()) for key in (
        'attempts', 'reported_calls', 'reported_tokens', 'unknown_call_attempts',
        'unknown_token_attempts', 'active_attempts')}
    usage_report['cost'] = dict(status='unknown', amount=None, currency=None)
    return {**batch, 'members': members, 'summary': summary,
            'capabilities': {'gemini_configured': not _keys_message(api.context().KEYS)},
            'current_allocated': totals, 'usage': usage, 'usage_report': usage_report,
            'continued_allowance': {key: totals[key] - initial[key] for key in totals},
            'allowance_semantics': 'Conservative request/token reservations, not actual token billing or monetary cost.',
            'deadline_semantics': 'Each child deadline begins when the worker starts this source; no completion ETA.'}


@router.post('', status_code=201)
def create_batch(body: BatchRequest):
    storage = api.store()
    config = body.model_dump()
    with _LOCK:
        existing = None
        # Idempotent retries also work after a source is removed or changes state.
        with storage._transaction() as db:
            for batch in storage._list(db, 'collection_batch'):
                if batch['request_id'] == body.request_id:
                    if batch['configuration'] != config:
                        raise HTTPException(409, 'Batch request ID reused with different inputs.')
                    existing = batch
                    break
        if existing is not None:
            return public_batch(storage, existing)
        sources = _sources(body.source_ids)
        budget = dict(calls=body.calls_per_source, tokens=body.calls_per_source * RESERVATION_TOKENS)
        batch = dict(id=uuid.uuid4().hex, request_id=body.request_id, status='draft',
                     configuration=config, created_at=time.time(), members=[],
                     initial_allocated={key: value * len(sources) for key, value in budget.items()})
        with storage._transaction() as db:
            for sid in body.source_ids:
                child = dict(id=uuid.uuid4().hex, batch_id=batch['id'], source_ids=[sid],
                             constraints=body.child_constraints(sid), budget=dict(budget),
                             usage=dict(tokens=0, calls=0), status='open', created_at=time.time())
                storage._save(db, 'collection', child)
                batch['members'].append(dict(source_id=sid, title=sources[sid].get('title', sid),
                                             collection_id=child['id'], error=None))
            storage._save(db, 'collection_batch', batch)
        return public_batch(storage, batch)


@router.get('')
def list_batches():
    storage = api.store()
    with storage._transaction() as db:
        batches = storage._list(db, 'collection_batch')
    return [public_batch(storage, batch) for batch in batches]


@router.get('/{batch_id}')
def get_batch(batch_id: str):
    storage = api.store()
    return public_batch(storage, _get(storage, batch_id))


def _dispatch(storage, sources, executor, batch):
    for member in batch['members']:
        identity = (str(storage.root), batch['id'], member['collection_id'])
        with _LOCK:
            current = _get(storage, batch['id'])
            child = storage.get_collection(member['collection_id'])
            if current['status'] != 'started' or child.get('current_run') or child['status'] == 'cancelled' or identity in _PENDING:
                continue
            _PENDING.add(identity)
            try:
                telemetry.submit(
                    executor, 'collection_generate', _start_member, storage, sources, executor, identity,
                    collection_id=child['id'], batch_id=batch['id'], source_ids=child['source_ids'],
                    synthetic=any(sources.get(sid).get('synthetic', False) for sid in child['source_ids']))
            except Exception:
                _PENDING.discard(identity)
                target = next(m for m in current['members'] if m['collection_id'] == identity[2])
                target['error'] = 'start_failed'
                _save(storage, current)


def _start_member(storage, sources, executor, identity):
    from .collection_generation import start_generation
    from .collection_workflow import dispatch
    try:
        with _LOCK:
            batch = _get(storage, identity[1])
            member = next(m for m in batch['members'] if m['collection_id'] == identity[2])
            child = storage.get_collection(identity[2])
            if batch['status'] != 'started' or child.get('current_run') or child['status'] == 'cancelled':
                return
            try:
                source = sources.get(member['source_id'])
                if source.get('status') != 'ready' or not source.get('words'):
                    raise ValueError('source_unavailable')
                start_generation(storage, sources, child['id'], executor,
                                 request_id='batch-' + batch['id'],
                                 calls=batch['configuration']['calls_per_source'],
                                 seconds=batch['configuration']['seconds_per_source'],
                                 dispatch_work=False)
                member['error'] = None
            except Exception:
                member['error'] = 'start_failed'
                _save(storage, batch)
                return
            _save(storage, batch)
        dispatch(storage, sources, identity[2], executor)
    finally:
        with _LOCK:
            _PENDING.discard(identity)


@router.post('/{batch_id}/start', status_code=202)
def start_batch(batch_id: str):
    storage = api.store()
    with _LOCK:
        batch = _get(storage, batch_id)
        if batch['status'] == 'cancelled':
            raise HTTPException(409, 'Batch is cancelled.')
        if (message := _keys_message(api.context().KEYS)):
            raise HTTPException(422, message)
        batch['status'] = 'started'
        _save(storage, batch)
    _dispatch(storage, api.context().STORE, api.context().WORKER, batch)
    return public_batch(storage, _get(storage, batch_id))


@router.post('/{batch_id}/cancel')
def cancel_batch(batch_id: str):
    storage = api.store()
    with _LOCK:
        batch = _get(storage, batch_id)
        batch['status'] = 'cancelled'
        _save(storage, batch)
        for member in batch['members']:
            child = storage.get_collection(member['collection_id'])
            run = storage.get_run(child['current_run']) if child.get('current_run') else None
            if (not run or run['status'] not in {'completed', 'needs_attention'}
                    or any(task['status'] in {'queued', 'active'} for task in storage.list_tasks(child['id']))):
                storage.cancel_collection(child['id'])
    return public_batch(storage, batch)


def resume_batches(root, sources, executor):
    if not (root / 'collections.sqlite3').exists():
        return
    storage = CollectionStore(root)
    with storage._transaction() as db:
        batches = storage._list(db, 'collection_batch')
    for batch in batches:
        if batch['status'] == 'cancelled':
            # Complete a cancellation interrupted after its durable dispatch fence.
            for member in batch['members']:
                child = storage.get_collection(member['collection_id'])
                run = storage.get_run(child['current_run']) if child.get('current_run') else None
                if (not run or run['status'] not in {'completed', 'needs_attention'}
                    or any(task['status'] in {'queued', 'active'} for task in storage.list_tasks(child['id']))):
                    storage.cancel_collection(child['id'])
        elif batch['status'] == 'started' and not _keys_message(api.context().KEYS):
            _dispatch(storage, sources, executor, batch)


def _keys_message(keys):
    from .models.gateway import keys_message
    return keys_message(keys)
