"""Upgrade/restore checks use synthetic data roots, no real media or credentials."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

import pytest

from clippie import desktop, storage_safety as safety


def metadata(root, *, format_version=1, build='old', status='ready'):
    safety.atomic_json(root / safety.VERSION_FILE, {'format_version': format_version, 'minimum_reader': format_version,
                      'root_key': safety.root_key(root), 'status': status, 'build_id': build, 'owner_pid': os.getpid()})


def make_data(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in safety.KNOWN_DATABASES:
        with closing(sqlite3.connect(root / name)) as db:
            db.execute('CREATE TABLE saved (id TEXT PRIMARY KEY, value TEXT)')
            db.execute('INSERT INTO saved VALUES (?,?)', ('kept', name))
            db.commit()
    for name, body in {'uploads/source.mp4': b'source media', 'models/weights.bin': b'model weights',
                       'model-calls/ab/request.json.gz': b'audit blob', 'studio/brand-assets/logo.png': b'brand asset',
                       'collection-artifacts/revision/output.mp4': b'export', 'topic-maps/map.json': b'{}',
                       'source-evidence/evidence.json': b'{}', 'model-routing.json': b'{"saved":true}'}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def rows(root):
    result = {}
    for name in safety.KNOWN_DATABASES:
        with closing(sqlite3.connect((root / name).as_uri() + '?mode=ro', uri=True)) as db:
            result[name] = db.execute('SELECT * FROM saved').fetchall()
    return result


def test_requires_real_lock_not_just_file(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    (root / safety.LOCK_FILE).write_text(str(os.getpid()))
    with pytest.raises(safety.DataSafetyError, match='lock'):
        safety.prepare_startup(root, build_id='build')
    assert not (root / safety.VERSION_FILE).exists()


def test_empty_root_adoption_and_same_build_reopen_need_no_backup(tmp_path):
    root = tmp_path / 'data'
    with desktop.instance_lock(root):
        pending = safety.prepare_startup(root, build_id='first')
        assert pending['status'] == 'starting' and pending['backup_id'] is None
        safety.complete_startup(root)
        assert safety.version(root)['status'] == 'ready'
        assert safety.prepare_startup(root, build_id='first')['status'] == 'ready'
        assert not safety.recovery_store(root).exists()


def test_future_format_refuses_before_loader_and_leaves_bytes(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    metadata(root, format_version=2)
    before = (root / safety.VERSION_FILE).read_bytes()
    with pytest.raises(safety.DataSafetyError) as error:
        desktop.run(root, loader=lambda: pytest.fail('future data reached app imports'))
    assert error.value.code == 'data_too_new'
    assert (root / safety.VERSION_FILE).read_bytes() == before
    assert not (root / 'clippie.sqlite3').exists()


def test_interrupted_upgrade_requires_explicit_same_build_retry(tmp_path):
    root = tmp_path / 'data'
    with desktop.instance_lock(root):
        safety.prepare_startup(root, build_id='interrupted')
        with pytest.raises(safety.DataSafetyError, match='Retry'):
            safety.prepare_startup(root, build_id='interrupted')
        with pytest.raises(safety.DataSafetyError):
            safety.prepare_startup(root, build_id='different', retry=True)
        safety.prepare_startup(root, build_id='interrupted', retry=True)
        safety.complete_startup(root)
    assert safety.version(root)['status'] == 'ready'


def test_all_six_databases_and_non_database_state_are_snapshotted(tmp_path):
    root = tmp_path / 'data'
    make_data(root)
    original = rows(root)
    with desktop.instance_lock(root):
        pending = safety.prepare_startup(root, build_id='new')
        backup, manifest = safety.checked_snapshot(root, pending['backup_id'])
        assert rows(backup / 'payload') == original
        for name in ('uploads/source.mp4', 'models/weights.bin', 'model-calls/ab/request.json.gz', 'studio/brand-assets/logo.png'):
            assert (backup / 'payload' / name).read_bytes() == (root / name).read_bytes()
        assert not (backup / 'payload' / safety.LOCK_FILE).exists()
        assert len([entry for entry in manifest['entries'] if entry['kind'] == 'database']) == 6
        safety.complete_startup(root)


def test_committed_wal_rows_survive_sqlite_backup(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    with closing(sqlite3.connect(root / 'clippie.sqlite3')) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE saved(value TEXT)')
        writer.execute('INSERT INTO saved VALUES (?)', ('only in WAL',))
        writer.commit()
        assert (root / 'clippie.sqlite3-wal').exists()
        with desktop.instance_lock(root):
            ident = safety.snapshot(root, reason='wal-test')
            path, _ = safety.checked_snapshot(root, ident)
            with closing(sqlite3.connect(path / 'payload/clippie.sqlite3')) as copied:
                assert copied.execute('SELECT value FROM saved').fetchone()[0] == 'only in WAL'
            assert not list((path / 'payload').glob('*-wal'))


def test_backup_failure_does_not_advance_version(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    make_data(root)
    metadata(root)
    before = (root / safety.VERSION_FILE).read_bytes()
    def fail(*a, **kw):
        raise OSError('fixture disk full')
    monkeypatch.setattr(safety, 'clone_or_copy', fail)
    with desktop.instance_lock(root), pytest.raises(safety.DataSafetyError):
        safety.prepare_startup(root, build_id='new')
    assert (root / safety.VERSION_FILE).read_bytes() == before
    assert not any(safety.recovery_store(root).glob('[a-f0-9]' * 32))


def test_restore_is_complete_keeps_rescue_and_keeps_same_lock_inode(tmp_path):
    root = tmp_path / 'data'
    make_data(root)
    metadata(root)
    expected = rows(root)
    with desktop.instance_lock(root):
        ident = safety.snapshot(root, reason='fixture')
        with closing(sqlite3.connect(root / 'clippie.sqlite3')) as db:
            db.execute('UPDATE saved SET value=?', ('new edits',))
            db.commit()
        (root / 'uploads/source.mp4').write_bytes(b'new media')
        (root / 'new-after-backup').write_text('rescue this')
        inode = (root / safety.LOCK_FILE).stat().st_ino
        result = safety.restore(root, ident)
        assert rows(root) == expected
        assert (root / 'uploads/source.mp4').read_bytes() == b'source media'
        assert not (root / 'new-after-backup').exists()
        rescue, _ = safety.checked_snapshot(root, result['rescue_id'])
        assert (rescue / 'payload/new-after-backup').read_text() == 'rescue this'
        assert (rescue / 'payload/uploads/source.mp4').read_bytes() == b'new media'
        assert (root / safety.LOCK_FILE).stat().st_ino == inode
        safety.require_lock(root)
        with pytest.raises(desktop.InstanceAlreadyRunning):
            with desktop.instance_lock(root):
                pass
        assert safety.background_resume_disabled(root)


def test_absent_at_snapshot_database_does_not_survive_restore(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'notes').write_text('kept')
    with desktop.instance_lock(root):
        ident = safety.snapshot(root, reason='before first DB')
        with closing(sqlite3.connect(root / 'studio.sqlite3')) as db:
            db.execute('CREATE TABLE newer(value TEXT)')
            db.commit()
        result = safety.restore(root, ident)
        assert not (root / 'studio.sqlite3').exists()
        rescue, _ = safety.checked_snapshot(root, result['rescue_id'])
        assert (rescue / 'payload/studio.sqlite3').exists()


@pytest.mark.parametrize('damaged', ['clippie.sqlite3', safety.VERSION_FILE])
def test_restore_healthy_copy_preserves_damaged_current_state(tmp_path, damaged):
    root = tmp_path / 'data'
    make_data(root)
    metadata(root)
    with desktop.instance_lock(root):
        ident = safety.snapshot(root, reason='healthy')
        (root / damaged).write_bytes(b'damaged state to retain')
        result = safety.restore(root, ident)
        assert rows(root)
        assert safety.version(root)['status'] == 'ready'
        rescue = safety.recovery_store(root) / result['rescue_id']
        assert (rescue / 'payload' / damaged).read_bytes() == b'damaged state to retain'
        assert safety.load_json(rescue / 'manifest.json')['rescue_only']
        assert not next(item for item in safety.recovery_info(root)['backups'] if item['id'] == result['rescue_id'])['can_restore']
        with pytest.raises(safety.DataSafetyError):
            safety.checked_snapshot(root, result['rescue_id'])


def test_corrupt_backup_rejects_restore_before_rescue_or_replacement(tmp_path):
    root = tmp_path / 'data'
    make_data(root)
    with desktop.instance_lock(root):
        ident = safety.snapshot(root, reason='fixture')
        path = safety.recovery_store(root) / ident / 'payload/uploads/source.mp4'
        path.write_bytes(b'changed')
        with pytest.raises(safety.DataSafetyError, match='incomplete'):
            safety.restore(root, ident)
        assert (root / 'uploads/source.mp4').read_bytes() == b'source media'


def test_manifest_path_escape_is_rejected(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'data').write_text('safe')
    with desktop.instance_lock(root):
        ident = safety.snapshot(root, reason='fixture')
        path = safety.recovery_store(root) / ident / 'manifest.json'
        value = json.loads(path.read_text())
        value['entries'][0]['path'] = '../outside'
        path.write_text(json.dumps(value))
        with pytest.raises(safety.DataSafetyError):
            safety.restore(root, ident)


def test_data_folder_cannot_be_home_or_filesystem_root(tmp_path):
    for root in (Path.home(), Path('/')):
        with pytest.raises(safety.DataSafetyError):
            safety.safe_root(root)


def test_changed_source_during_snapshot_is_detected(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir()
    path = root / 'notes'
    path.write_text('old')
    original = safety.clone_or_copy
    def concurrent(source, destination, progress=None):
        original(source, destination, progress)
        source.write_text('concurrent change')
    monkeypatch.setattr(safety, 'clone_or_copy', concurrent)
    with desktop.instance_lock(root), pytest.raises(safety.DataSafetyError, match='changed'):
        safety.snapshot(root, reason='fixture')


def test_acknowledgment_enables_only_next_start_and_preserves_history(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    safety.atomic_json(root / safety.RESTORE_FILE, {'backup_id': 'a' * 32, 'background_resume_disabled': True})
    assert safety.background_resume_disabled(root)
    safety.acknowledge_restore(root)
    assert not safety.background_resume_disabled(root)
    assert safety.restore_notice(root)['backup_id'] == 'a' * 32


def test_store_constructors_refuse_future_data_without_creating_tables(tmp_path):
    from clippie.store import Store
    from clippie.collection_store import CollectionStore
    from clippie.studio_store import StudioStore
    from clippie.publish.store import PublishStore
    from clippie.models.store import CallLog
    root = tmp_path / 'data'
    root.mkdir()
    metadata(root, format_version=2)
    for constructor in (Store, CollectionStore, StudioStore, PublishStore):
        with pytest.raises(safety.DataSafetyError):
            constructor(root)
    with pytest.raises(safety.DataSafetyError):
        CallLog(root)._connect()
    assert not list(root.glob('*.sqlite3'))


def test_failed_atomic_exchange_leaves_current_data_intact_and_rescue_exists(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    make_data(root)
    with desktop.instance_lock(root):
        ident = safety.snapshot(root, reason='fixture')
        (root / 'uploads/source.mp4').write_bytes(b'latest')
        def fail(*args):
            raise safety.DataSafetyError('restore_failed', 'No atomic exchange on fixture filesystem')
        monkeypatch.setattr(safety, 'atomic_swap', fail)
        with pytest.raises(safety.DataSafetyError):
            safety.restore(root, ident)
        assert (root / 'uploads/source.mp4').read_bytes() == b'latest'
        assert len(safety.recovery_info(root)['backups']) == 2


def test_restore_quarantine_does_not_refund_usage_or_republish(tmp_path):
    from clippie.collection_store import CollectionStore
    from clippie.publish.store import PublishStore
    root = tmp_path / 'data'
    collection = CollectionStore(root)
    project = collection.create_collection(['source'], budget={'tokens': 1000, 'calls': 10})
    first = collection.enqueue_task(project['id'], 'map', 'v1', 'one', reserved_tokens=100, reserved_calls=1)
    active = collection.claim_task(now=1)
    second = collection.enqueue_task(project['id'], 'discover', 'v1', 'two')
    run = collection.start_run(project['id'], {}, 'request')
    before = collection.get_collection(project['id'])['usage']
    publisher = PublishStore(root)
    channel = publisher.add_channel('youtube', 'youtube', 'Fixture', 'UC-fixture', [], 'fixture-reference')
    pending = publisher.create_post(platform='youtube', channel_id=channel['id'], short_id='s', revision_id='r', title='Pending', status='scheduled')
    published = publisher.create_post(platform='youtube', channel_id=channel['id'], short_id='s', revision_id='r', title='Published', status='published', external_id='existing-video')
    safety.atomic_json(root / safety.RESTORE_FILE, {'backup_id': 'b' * 32, 'background_resume_disabled': True})
    safety.quarantine_restored_work(root)
    assert collection.get_task(first['id'])['status'] == collection.get_task(second['id'])['status'] == 'failed'
    assert collection.get_task(first['id'])['lease_token'] is None
    assert collection.list_attempts(first['id'])[0]['status'] == 'expired'
    assert collection.get_collection(project['id'])['usage'] == before
    assert collection.get_run(run['id'])['status'] == 'needs_attention'
    assert publisher.get_post(pending['id'])['status'] == 'draft'
    assert publisher.get_post(published['id'])['external_id'] == 'existing-video'
    assert publisher.get_post(published['id'])['status'] == 'published'
    # Explicit work performed after quarantine is not undone on each restart.
    publisher.update_post(pending['id'], status='scheduled')
    safety.quarantine_restored_work(root)
    assert publisher.get_post(pending['id'])['status'] == 'scheduled'


def test_recovery_api_requires_review_and_never_dispatches(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from clippie import recovery_api
    root = tmp_path / 'data'
    root.mkdir()
    safety.atomic_json(root / safety.RESTORE_FILE, {'backup_id': 'b' * 32, 'background_resume_disabled': True})
    monkeypatch.setattr(recovery_api, 'root', lambda: root)
    app = FastAPI()
    app.include_router(recovery_api.router)
    client = TestClient(app)
    assert client.get('/api/system/recovery').json()['paused']
    assert client.post('/api/system/recovery/acknowledge', json={'reviewed': False}).status_code == 422
    response = client.post('/api/system/recovery/acknowledge', json={'reviewed': True})
    assert response.json()['restart_required']
    assert not response.json()['paused']


def test_progress_protocol_is_throttled(capsys):
    report = desktop.progress_reporter()
    for _ in range(1000):
        report('Local copy in progress')
    assert capsys.readouterr().out.count('startup_progress') == 1
