from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from clippie import telemetry, telemetry_api


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    rec = telemetry.Recorder(tmp_path/'private data')
    monkeypatch.setattr(telemetry, 'recorder', lambda root=None: rec)
    yield rec
    rec.shutdown()


def rows(rec, table):
    assert rec.flush()
    return telemetry_api.decoded(rec.read(f'SELECT * FROM {table}'))


def test_connected_worker_attempts_queue_stages_and_cache_metrics(recorder):
    token = telemetry.OBSERVATION.set('qa')
    gate = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(lambda: gate.wait(2))
        first = telemetry.begin('production', production_id='p1', source_ids=['s1'], attempt=1, auto_render=True)
        def work():
            with telemetry.stage('index', source_id='s1'):
                telemetry.attributes(cache='hit', cache_kind='index')
            with telemetry.stage('planning', planner='baseline'):
                telemetry.update(revision=1, actual_duration=43.8)
            telemetry.event('planner_repair', reason='invalid_output', attempt=2)
            return 'video-ready'
        future = telemetry.submit(pool, 'production', work, telemetry_run=first)
        time.sleep(.025)
        gate.set()
        assert future.result() == 'video-ready'
        retry = telemetry.begin('render', production_id='p1', previous_run_id=first.id, attempt=2, revision=1)
        telemetry.submit(pool, 'render', lambda: None, telemetry_run=retry).result()
    telemetry.OBSERVATION.reset(token)
    attempts = rows(recorder, 'runs')
    assert len(attempts) == 2 and {r['observation'] for r in attempts} == {'qa'}
    assert attempts[0]['status'] == 'success'
    assert attempts[0]['attributes']['cache_state'] == 'warm'
    assert attempts[1]['attributes']['previous_run_id'] == first.id
    spans = rows(recorder, 'spans')
    q = next(s for s in spans if s['name'] == 'queue' and s['trace_id'] == attempts[0]['trace_id'])
    assert q['end_ns']-q['start_ns'] >= 20_000_000
    root = next(s for s in spans if s['name'] == 'workflow' and s['trace_id'] == attempts[0]['trace_id'])
    assert all(s['parent_id'] == root['id'] for s in spans if s['trace_id'] == root['trace_id'] and s['name'] != 'workflow')
    assert rows(recorder, 'events')[0]['observation'] == 'qa'
    report = telemetry_api.summary('qa', 30)
    assert report['ready_video_attempts'] == report['video_attempts'] == 1
    assert report['performance'][0]['cache_state'] == 'warm'
    assert telemetry_api.summary('user', 30)['attempts'] == 0


def test_failure_and_restart_preserve_attempt_identity_without_content(recorder, monkeypatch):
    secret = '/Users/private/transcript.txt?key=secret transcript words'
    with pytest.raises(ValueError):
        with telemetry.operation('production', production_id='failed'):
            with telemetry.stage('planning'):
                raise ValueError(secret)
    failed = rows(recorder, 'runs')[0]
    assert failed['status'] == 'failed' and failed['attributes']['error_code'] == 'invalid_output'
    assert secret not in json.dumps(rows(recorder, 'spans'))
    queued = recorder.start('render', production_id='queued')
    assert recorder.flush()
    path = recorder.path
    recorder.shutdown()
    after = telemetry.Recorder(path.parent)
    try:
        assert after.flush()
        history = rows(after, 'runs')
        interrupted = next(r for r in history if r['id'] == queued.id)
        assert interrupted['status'] == 'interrupted' and interrupted['ended_ns']
        assert next(r for r in history if r['id'] == failed['id'])['status'] == 'failed'
        assert all(s['status'] != 'running' for s in rows(after, 'spans'))
    finally:
        after.shutdown()


def test_storage_failure_and_full_queue_never_block_work(tmp_path, monkeypatch):
    occupied = tmp_path/'not-a-directory'
    occupied.write_text('keep')
    rec = telemetry.Recorder(occupied, capacity=2)
    monkeypatch.setattr(telemetry, 'recorder', lambda root=None: rec)
    started = time.monotonic()
    with telemetry.operation('production'):
        for _ in range(50):
            with telemetry.stage('render'):
                pass
    assert time.monotonic()-started < 1
    assert rec.flush()
    assert rec.counters['dropped'] > 0 and rec.counters['storage_errors'] > 0
    rec.shutdown()
    assert not rec.thread.is_alive()
    assert occupied.read_text() == 'keep'


def test_exporter_and_tracer_runtime_failures_are_fail_open(recorder, monkeypatch):
    run = telemetry.begin('production')
    def broken(*args, **kwargs):
        raise RuntimeError('private-error-content')
    monkeypatch.setattr(recorder.tracer, 'start_span', broken)
    with recorder.execute(run):
        with telemetry.stage('render'):
            telemetry.attributes(prompt='private words')
            telemetry.update(revision=1)
        assert True
    assert recorder.counters['runtime_errors'] >= 2
    assert rows(recorder, 'runs')[0]['status'] == 'success'
    with pytest.raises(ValueError, match='task failure'):
        with telemetry.operation('production'):
            with telemetry.stage('render'):
                raise ValueError('task failure')


def test_allowlist_rejects_content_and_nonfinite_or_unsafe_metadata():
    attrs = telemetry.safe_attributes({'prompt':'hello','transcript':'words','source':'https://private',
        'api_key':'secret','error_message':'secret','genre':'a user sentence','model_id':'/private/model.bin',
        'source_ids':['https://private'], 'source_id':'/private/source.mp4', 'total_tokens':float('inf'),
        'model_revision':'https://private/token','brief':'text','revision':True,'peak_memory_gb':3.2,
        'planner':'baseline','style_id':'insight_cut','diagnostic_id':'a123'})
    assert attrs == {'peak_memory_gb':3.2,'planner':'baseline','style_id':'insight_cut','diagnostic_id':'a123'}


def test_editor_starts_when_recorder_initialization_fails(tmp_path, monkeypatch):
    from clippie import app as app_module
    from clippie.store import Store
    monkeypatch.setattr(app_module, 'STORE', Store(tmp_path/'data'))
    monkeypatch.setattr(app_module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    def unavailable(*args, **kwargs):
        raise RuntimeError('recorder unavailable')
    monkeypatch.setattr(telemetry, 'recorder', unavailable)
    with TestClient(app_module.app) as client:
        assert client.get('/api/jobs').status_code == 200
        assert client.get('/api/settings').status_code == 200


def test_quality_denominators_latest_revision_review_and_saved_edits(recorder):
    telemetry.event('review_saved',production_id='p1',revision=1,rating='revise')
    telemetry.event('review_saved',production_id='p1',revision=1,rating='keep')
    telemetry.event('review_saved',production_id='p1',revision=2,rating='reject')
    telemetry.event('download_clicked',production_id='p2',revision=1,kind='video')
    telemetry.event('timeline_saved',production_id='p1',revision=2,changed_clip_count=3)
    rows(recorder,'events')
    report=telemetry_api.summary('user',30)
    assert report['reviews'] == {'reviewed_revisions':2,'keep':1,'revise':0,'reject':1}
    assert report['editing']['saved_revisions'] == 1
    assert report['interactions']['download_clicked'] == 1


def test_retention_and_corrupt_metadata_do_not_touch_production_files(recorder):
    assert recorder.flush()
    media = recorder.path.parent/'keep.mp4'; media.write_bytes(b'original')
    with telemetry.operation('production',production_id='old'):
        pass
    saved=rows(recorder,'runs')[0]
    with sqlite3.connect(recorder.path) as db:
        db.execute('UPDATE runs SET created_ns=? WHERE id=?',(time.time_ns()-31*86400*10**9,saved['id']))
        recorder._prune(db)
    assert rows(recorder,'runs') == [] and rows(recorder,'spans') == []
    assert media.read_bytes() == b'original'
    assert telemetry_api.decoded([{'attributes':'not-json'},{'attributes':'[]'}]) == [{'attributes':{}},{'attributes':{}}]


def test_model_subprocess_continues_trace_without_content(recorder, monkeypatch, capsys):
    from io import StringIO
    from clippie import local_editor_worker
    with telemetry.operation('production') as run:
        with telemetry.stage('model'):
            parent=telemetry.propagation()
            monkeypatch.setattr(local_editor_worker.sys,'stdin',StringIO(json.dumps({'traceparent':parent,'messages':[]})))
            monkeypatch.setattr(local_editor_worker,'infer',lambda request:{'text':'private model output','usage':{'total_tokens':10}})
            local_editor_worker.main()
    child=json.loads(capsys.readouterr().out)['telemetry_span']
    assert child['trace_id'] == run.data['trace_id']
    assert child['parent_id'] == parent.split('-')[2]
    assert child['attributes'] == {} and 'private' not in json.dumps(child)
