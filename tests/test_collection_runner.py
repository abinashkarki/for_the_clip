import subprocess
import sys
import threading
import time

from clippie.collection_runner import run_one
from clippie.collection_store import CollectionStore


def setup_task(tmp_path, stage='test', **kwargs):
    store = CollectionStore(tmp_path)
    c = store.create_collection(['source'], budget={'tokens': 100, 'calls': 10})['id']
    task = store.enqueue_task(c, stage, 'v1', 'key', **kwargs)
    return store, c, task


def test_success_usage_and_heartbeat(tmp_path):
    store, c, task = setup_task(tmp_path, reserved_tokens=3)

    def handler(ctx):
        time.sleep(.16)
        # Another instance cannot recover this task despite its initial tiny lease.
        assert CollectionStore(tmp_path).recover_expired() == []
        ctx.checkpoint()
        return {'result': {'ok': True}, 'actual_tokens': 7, 'actual_calls': 1}

    result = run_one(store, {'test': handler}, lease_seconds=.06)
    assert result['status'] == 'completed'
    assert result['result'] == {'ok': True}
    assert store.get_collection(c)['usage'] == {'tokens': 7, 'calls': 1}
    assert not any(t.name == 'collection-lease' for t in threading.enumerate())


def test_cancel_terminates_owned_child_and_reconciles_late_usage(tmp_path):
    store, c, task = setup_task(tmp_path, reserved_tokens=2)
    started = threading.Event()
    processes = []
    results = []

    def handler(ctx):
        process = ctx.register_process(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']))
        processes.append(process)
        started.set()
        process.wait(timeout=3)
        return {'result': {'must_not_commit': True}, 'actual_tokens': 5}

    thread = threading.Thread(target=lambda: results.append(run_one(store, {'test': handler}, lease_seconds=.3)))
    thread.start()
    assert started.wait(2)
    CollectionStore(tmp_path).cancel_collection(c)
    thread.join(timeout=4)
    assert not thread.is_alive()
    assert processes[0].poll() is not None
    assert results[0]['status'] == 'cancelled'
    assert results[0]['result'] is None
    assert store.get_collection(c)['usage']['tokens'] == 5


def test_failure_is_fixed_code_and_next_sibling_runs(tmp_path):
    store, c, task = setup_task(tmp_path)
    sibling = store.enqueue_task(c, 'good', 'v1', 'sibling')

    def bad(ctx):
        raise RuntimeError('private content must not persist')

    first = run_one(store, {'test': bad})
    assert first['status'] == 'failed'
    assert first['error'] == 'handler_failed'
    second = run_one(store, {'good': lambda ctx: {'result': 'ok'}})
    assert second['id'] == sibling['id']
    assert second['status'] == 'completed'
    assert run_one(store, {}) is None


def test_unknown_and_invalid_handlers_are_actionable(tmp_path):
    store, c, task = setup_task(tmp_path)
    assert run_one(store, {})['error'] == 'handler_unavailable'
    store.retry_task(task['id'])
    assert run_one(store, {'test': lambda ctx: {'result': {}, 'actual_tokens': 1.5}})['error'] == 'handler_invalid_result'


def test_restart_adopts_committed_revision_without_repeating_handler(tmp_path):
    store, c, task = setup_task(tmp_path)
    short = store.add_short(c)
    # Use a separate task tied to the short; first task isn't in this adoption path.
    store.cancel_collection(c)
    c = store.create_collection(['source'])['id']
    short = store.add_short(c)
    task = store.enqueue_task(c, 'plan', 'v1', 'plan', short_id=short['id'])
    claim = store.claim_task(collection_id=c, lease_seconds=1)
    revision = store.append_revision(short['id'], {'title': 'saved'}, task_id=task['id'],
                                    lease_token=claim['lease_token'], input_version='v1')
    store.recover_expired(now=claim['lease_until'])

    def must_not_run(ctx):
        raise AssertionError('handler rerun')

    result = run_one(CollectionStore(tmp_path), {'plan': must_not_run}, collection_id=c)
    assert result['status'] == 'completed'
    assert result['result'] == {'revision_id': revision['id'], 'adopted': True}
    assert len(store.list_revisions(short['id'])) == 1


def test_restart_adopts_held_review_without_model_or_false_usage(tmp_path):
    storage = CollectionStore(tmp_path)
    c = storage.create_collection(['source'], budget={'tokens': 100, 'calls': 5})['id']
    short = storage.add_short(c)
    task = storage.enqueue_task(c, 'review_short', 'v1', 'held-review', short_id=short['id'],
                                reserved_tokens=10, reserved_calls=1)
    claim = storage.claim_task(collection_id=c, lease_seconds=1)
    review = {'status': 'uncertain', 'issues': [{'severity': 'warning', 'category': 'meaning'}]}
    revision = storage.append_revision(short['id'], {'inspection_only': True, 'technical': 'pass',
        'editorial': review}, task_id=task['id'], lease_token=claim['lease_token'], input_version='v1', promote=False)
    storage.recover_expired(now=claim['lease_until'])
    def must_not_call(ctx):
        raise AssertionError('Recovered held review must not invoke model')
    result = run_one(CollectionStore(tmp_path), {'review_short': must_not_call}, collection_id=c)
    assert result['status'] == 'completed'
    assert result['result'] == {**review, 'revision_id': revision['id'], 'adopted': True}
    assert len(storage.list_revisions(short['id'])) == 1
    assert storage.get_short(short['id'])['held_revision'] == revision['id']
    attempts = storage.list_attempts(task['id'])
    assert len(attempts) == 2
    assert all(attempt['reported_usage']['tokens'] is None for attempt in attempts)
    assert storage.get_collection(c)['usage'] == {'tokens': 20, 'calls': 2}
