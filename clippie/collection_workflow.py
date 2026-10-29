"""Local evidence preparation on the existing bounded worker, before editorial work."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from . import source_evidence, telemetry
from .collection_runner import run_one
from .collection_store import CollectionStore

_TIMERS = set()
_COORDINATORS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='collection-graph')
_DISPATCHED = {}
_REDISPATCH = set()
_LOCK = threading.RLock()


def queue_preparation(storage, sources, collection_id, executor):
    collection = storage.get_collection(collection_id)
    if collection['status'] == 'cancelled':
        raise ValueError('Collection cancelled')
    # Compute every input before enqueue so malformed evidence does not queue half a group.
    inputs = [(source_id, source_evidence.evidence_key(sources.get(source_id)))
              for source_id in collection['source_ids']
              if collection['constraints'].get('source_roles', {}).get(source_id, 'primary') == 'primary']
    for source_id, key in inputs:
        storage.enqueue_task(collection_id, 'prepare_source', key, f'prepare-{source_id}-{key}',
                             payload={'source_id': source_id}, reserved_tokens=0, reserved_calls=0)
    dispatch(storage, sources, collection_id, executor)
    return storage.get_collection(collection_id)


def dispatch(storage, sources, collection_id, executor):
    from .collection_generation import drain_generation
    generating = bool(storage.get_collection(collection_id).get('current_run'))
    source_ids = storage.get_collection(collection_id)['source_ids']
    identity = (str(storage.root), collection_id)
    with _LOCK:
        existing = _DISPATCHED.get(identity)
        if existing is not None and not existing.done():
            _REDISPATCH.add(identity)
            return existing
        future = telemetry.submit(_COORDINATORS if generating else executor, 'collection_generate' if generating else 'collection_prepare',
                                drain_generation if generating else drain_preparation,
                                storage, sources, collection_id, executor, collection_id=collection_id,
                                batch_id=storage.get_collection(collection_id).get('batch_id'),
                                source_ids=source_ids, synthetic=any(sources.get(sid).get('synthetic', False) for sid in source_ids))
        _DISPATCHED[identity] = future
        if future is not None:
            def finished(done):
                with _LOCK:
                    if _DISPATCHED.get(identity) is done:
                        _DISPATCHED.pop(identity, None)
                    retry = identity in _REDISPATCH
                    _REDISPATCH.discard(identity)
                if retry:
                    try:
                        dispatch(storage, sources, collection_id, executor)
                    except RuntimeError:
                        pass  # Shutdown leaves durable work for startup recovery.
            future.add_done_callback(finished)
        return future



def preparation_handler(storage, sources, collection_id):
    def prepare(ctx):
        source_id = ctx.task['payload']['source_id']
        source = sources.get(source_id)
        if source_evidence.evidence_key(source) != ctx.task['input_version']:
            raise ValueError('Evidence input changed')
        ctx.checkpoint()
        with telemetry.stage('source_evidence', collection_id=collection_id, task_id=ctx.task['id'],
                             source_id=source_id, attempt=ctx.task['attempts']):
            prepared = source_evidence.prepare_evidence(source, storage.root / 'source-evidence')
            telemetry.attributes(cache_kind='evidence', cache='hit' if prepared['cache_status'] == 'hit' else 'miss')
        ctx.checkpoint()
        if source_evidence.evidence_key(sources.get(source_id)) != ctx.task['input_version']:
            raise ValueError('Evidence input changed')
        return {'result': {'source_id': source_id, 'artifact_key': prepared['cache_artifact_key'],
                           'cache': prepared['cache_status'], 'unit_count': len(prepared['artifact']['units'])},
                'actual_tokens': 0, 'actual_calls': 0}
    return prepare


def drain_preparation(storage, sources, collection_id, executor):
    prepare = preparation_handler(storage, sources, collection_id)

    while run_one(storage, {'prepare_source': prepare}, collection_id=collection_id) is not None:
        pass
    schedule_recheck(storage, sources, collection_id, executor)


def schedule_recheck(storage, sources, collection_id, executor):
    # After a restart another owner may still have a valid lease. Recheck at expiry;
    # do not assume an observation timeout or process restart authorizes duplicate work.
    active = [task for task in storage.list_tasks(collection_id) if task['status'] == 'active']
    if active:
        delay = max(.05, min(task['lease_until'] for task in active) - time.time() + .05)
        timer = None

        def retry_after_expiry():
            with _LOCK:
                _TIMERS.discard(timer)
            try:
                dispatch(storage, sources, collection_id, executor)
            except RuntimeError:  # Executor stopped; persisted work resumes next start.
                pass

        timer = threading.Timer(delay, retry_after_expiry)
        timer.daemon = True
        with _LOCK:
            _TIMERS.add(timer)
        timer.start()


def resume_preparation(root, sources, executor):
    if not (root / 'collections.sqlite3').exists():
        return
    storage = CollectionStore(root)
    for collection in storage.list_collections():
        tasks = storage.list_tasks(collection['id'])
        run = storage.get_run(collection['current_run']) if collection.get('current_run') else None
        if collection['status'] != 'cancelled' and ((run and run['status'] == 'active') or
                any(task['status'] in {'queued', 'active'} for task in tasks)):
            dispatch(storage, sources, collection['id'], executor)


def stop_preparation():
    with _LOCK:
        for timer in _TIMERS:
            timer.cancel()
        _TIMERS.clear()
