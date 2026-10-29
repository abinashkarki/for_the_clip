"""Installer lifecycle tests with tiny verified artifacts, no model/network calls."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clippie import model_setup as setup, model_setup_api
from clippie.model_downloads import DownloadCancelled

BODY = b'verified model fixture'


def entry(ident='vision', *, directory=False):
    return {'id': ident, 'label': ident, 'description': 'fixture', 'required': True,
            'target': ident + ('-model' if directory else '.bin'), 'layout': 'directory' if directory else 'file',
            'override': 'CLIPPIE_SETUP_TEST_OVERRIDE', 'dependencies': ['fixture'],
            'source_url': 'https://publisher.invalid/model', 'terms_url': 'https://publisher.invalid/terms',
            'required_disk_bytes': 1024, 'required_files': ['weights.bin'],
            'artifacts': [{'name': 'weights.bin', 'url': 'https://publisher.invalid/weights.bin',
                           'size': len(BODY), 'sha256': hashlib.sha256(BODY).hexdigest()}]}


def download(artifact, destination, *, cancel, progress):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(BODY)
    progress(len(BODY), len(BODY))
    return destination


def manager(tmp_path, *, models=None, downloader=download, converter=None):
    kwargs = dict(models=models or [entry()], downloader=downloader, dependency_check=lambda _: True)
    if converter:
        kwargs['converter'] = converter
    return setup.ModelSetup(tmp_path, **kwargs)


def finish(item):
    item.thread.join(timeout=5)
    assert not item.thread.is_alive()
    return item.snapshot()['models'][0]


def test_status_is_read_only_and_catalog_is_fully_pinned(tmp_path):
    item = manager(tmp_path)
    assert item.snapshot()['models'][0]['state'] == 'missing'
    assert not item.work.exists()
    models = setup.catalog()
    assert {m['id'] for m in models} == {'whisper', 'embeddings', 'vision'}
    assert sum(len(m['artifacts']) for m in models) == 15
    assert all(len(a['sha256']) == 64 and a['size'] > 0 for m in models for a in m['artifacts'])


def test_success_is_atomic_and_verified(tmp_path):
    item = manager(tmp_path)
    item.start('vision', accept_terms=True)
    status = finish(item)
    assert status['ready'] and status['verified'] and status['state'] == 'ready'
    assert item.target(entry()).read_bytes() == BODY
    assert item.receipt(entry()).is_file()


def test_changed_setup_owned_file_requires_preparation(tmp_path):
    item = manager(tmp_path)
    item.start('vision', accept_terms=True)
    finish(item)
    item.target(entry()).write_bytes(b'broken')
    assert not item.snapshot()['models'][0]['ready']


def test_bad_download_cannot_be_promoted(tmp_path):
    def bad(*args, **kwargs):
        path = download(*args, **kwargs)
        path.write_bytes(b'bad')
        return path
    item = manager(tmp_path, downloader=bad)
    item.start('vision', accept_terms=True)
    status = finish(item)
    assert status['state'] == 'error' and not status['ready']
    assert not item.target(entry()).exists()


def test_cancel_retains_partial_bytes_and_cross_process_lock(tmp_path):
    entered = threading.Event()
    def waiting(artifact, destination, *, cancel, progress):
        destination.parent.mkdir(parents=True, exist_ok=True)
        Path(str(destination) + '.part').write_bytes(b'verified')
        entered.set()
        cancel.wait(5)
        raise DownloadCancelled()
    first = manager(tmp_path, downloader=waiting)
    second = manager(tmp_path)
    first.start('vision', accept_terms=True)
    assert entered.wait(2)
    assert second.snapshot()['active'] == 'vision'
    with pytest.raises(setup.SetupError, match='Another model'):
        second.start('vision', accept_terms=True)
    first.cancel('vision')
    assert finish(first)['state'] == 'cancelled'
    assert list(first.work.rglob('*.part'))
    second.start('vision', accept_terms=True)
    assert finish(second)['verified']
    # first no longer hides another process's more recent durable job state.
    assert first.snapshot()['models'][0]['state'] == 'ready'


def test_interrupted_state_is_resumable(tmp_path):
    item = manager(tmp_path)
    setup.write_json(item.work / 'job.json', {'id': 'vision', 'state': 'downloading', 'bytes_received': 4})
    assert item.snapshot()['active'] is None
    assert item.snapshot()['models'][0]['state'] == 'interrupted'
    item.start('vision', accept_terms=True)
    assert finish(item)['ready']


def test_space_and_consent_checks_do_not_start_download(tmp_path, monkeypatch):
    item = manager(tmp_path)
    with pytest.raises(setup.SetupError) as denied:
        item.start('vision', accept_terms=False)
    assert denied.value.status == 422 and not item.work.exists()
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(setup.SetupError) as disk:
        item.start('vision', accept_terms=True)
    assert disk.value.status == 507 and item.thread is None
    assert not item.active_elsewhere()


@pytest.mark.parametrize('relative', ['vision', 'vision/downloads', 'receipts'])
def test_staging_symlinks_cannot_write_outside_data_root(tmp_path, relative):
    root, outside = tmp_path / 'data', tmp_path / 'outside'
    outside.mkdir()
    protected = outside / 'keep'
    protected.write_text('untouched')
    item = manager(root)
    link = item.work / relative
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(setup.SetupError, match='linked'):
        item.start('vision', accept_terms=True)
    assert protected.read_text() == 'untouched'
    assert len(list(outside.iterdir())) == 1


def test_environment_managed_model_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv('CLIPPIE_SETUP_TEST_OVERRIDE', str(tmp_path / 'external'))
    item = manager(tmp_path / 'data')
    with pytest.raises(setup.SetupError, match='outside'):
        item.start('vision', accept_terms=True)
    assert not item.work.exists()


def test_promotion_journal_recovers_receipt_after_crash(tmp_path):
    model = entry()
    item = manager(tmp_path)
    target = item.target(model)
    target.parent.mkdir(parents=True)
    target.write_bytes(BODY)
    stat = target.stat()
    record = {'manifest_hash': item.manifest_hash(model), 'files': [{'name': 'weights.bin', 'size': len(BODY),
              'mtime_ns': stat.st_mtime_ns, 'sha256': hashlib.sha256(BODY).hexdigest()}]}
    setup.write_json(item.work / 'vision.promotion.json', record)
    assert not item.snapshot()['models'][0]['ready']
    item.start('vision', accept_terms=True)
    assert item.thread is None
    assert item.snapshot()['models'][0]['verified']
    assert item.snapshot()['models'][0]['state'] == 'ready'


def test_failed_promotion_restores_previous_partial_model(tmp_path, monkeypatch):
    model = entry(directory=True)
    item = manager(tmp_path, models=[model])
    target = item.target(model)
    target.mkdir(parents=True)
    (target / 'previous-note').write_text('preserved')
    real = os.replace
    def refuse(source, destination):
        if Path(source).name == 'payload' and Path(destination) == target:
            raise OSError('fixture promotion failure')
        return real(source, destination)
    monkeypatch.setattr(os, 'replace', refuse)
    item.start('vision', accept_terms=True)
    assert finish(item)['state'] == 'error'
    assert (target / 'previous-note').read_text() == 'preserved'


def test_conversion_is_staged_and_verified(tmp_path):
    model = entry('whisper', directory=True)
    model['conversion'] = 'fixture'
    def convert(source, target, cancel):
        assert source.read_bytes() == BODY
        (target / 'weights.bin').write_bytes(BODY)
    item = manager(tmp_path, models=[model], converter=convert)
    item.start('whisper', accept_terms=True)
    assert finish(item)['verified']


def test_setup_api_requires_explicit_consent_and_known_model(tmp_path, monkeypatch):
    item = manager(tmp_path)
    monkeypatch.setattr(model_setup_api, 'current', lambda: item)
    app = FastAPI()
    app.include_router(model_setup_api.router)
    client = TestClient(app)
    assert client.get('/api/setup/models').status_code == 200
    assert client.post('/api/setup/models/vision/install', json={'accept_terms': False}).status_code == 422
    assert client.post('/api/setup/models/vision/install', json={'accept_terms': 'yes'}).status_code == 422
    assert client.post('/api/setup/models/unknown/install', json={'accept_terms': True}).status_code == 422
    assert client.post('/api/setup/models/vision/install', json={'accept_terms': True}).status_code == 200
    assert finish(item)['verified']


def test_caption_health_matches_the_packaged_v3_renderer():
    from clippie.app import caption_overlay_support
    assert caption_overlay_support(' TSC overlay VV->V Overlay\n ... format V->V Pixel format')
    assert not caption_overlay_support(' ... ass V->V Render ASS subtitles')


def test_resume_does_not_require_download_space_twice(tmp_path, monkeypatch):
    model = entry()
    model['required_disk_bytes'] = 32 * 1024 * 1024
    # Use a larger synthetic declared artifact to exercise space accounting;
    # the worker downloader itself still gets the tiny verified fixture below.
    item = manager(tmp_path, models=[model])
    cache = item.work / 'vision/downloads/weights.bin.part'
    cache.parent.mkdir(parents=True)
    model['artifacts'][0]['size'] = 20 * 1024 * 1024
    with cache.open('wb') as stream:
        stream.truncate(20 * 1024 * 1024)
    assert item.additional_space(model) == 16 * 1024 * 1024
    monkeypatch.setattr(shutil, 'disk_usage', lambda _: SimpleNamespace(free=20 * 1024 * 1024))
    item.start('vision', accept_terms=True)  # preflight permits a resume
    finish(item)  # synthetic artifact size mismatch correctly prevents readiness
    assert not item.snapshot()['models'][0]['ready']


def test_cosmetic_app_updates_preserve_model_receipt_compatibility(tmp_path):
    model = entry()
    item = manager(tmp_path, models=[model])
    item.start('vision', accept_terms=True)
    assert finish(item)['verified']
    # Emulate the previous prototype's full-catalog fingerprint.
    receipt = setup.read_json(item.receipt(model))
    receipt['manifest_hash'] = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    setup.write_json(item.receipt(model), receipt)
    model['label'] = 'New display label'
    model['description'] = 'Updated help text'
    assert item.snapshot()['models'][0]['verified']
    model['artifacts'][0]['sha256'] = '0' * 64
    assert not item.snapshot()['models'][0]['ready']


def test_batch_sequential_verified_and_skips_ready(tmp_path):
    models = [entry('whisper'), entry('embeddings'), entry('vision')]
    calls = []
    def tracked(artifact, destination, **kwargs):
        calls.append(destination.parts[-3])
        return download(artifact, destination, **kwargs)
    item = manager(tmp_path, models=models, downloader=tracked)
    item.start('whisper', accept_terms=True)
    item.thread.join(timeout=5)
    selections = {m['id']: item.terms_identity(m) for m in models}
    item.start_batch(selections)
    item.batch_thread.join(timeout=5)
    assert item.snapshot()['batch']['state'] == 'completed'
    assert item.snapshot()['batch']['completed'] == list(selections)
    assert calls == ['whisper', 'embeddings', 'vision']
    assert all(row['verified'] for row in item.snapshot()['models'])


def test_batch_failure_resume_and_changed_terms(tmp_path):
    models = [entry('whisper'), entry('vision')]
    fail = [True]
    def flaky(artifact, destination, **kwargs):
        path = download(artifact, destination, **kwargs)
        if destination.parts[-3] == 'vision' and fail[0]:
            path.write_bytes(b'bad')
        return path
    item = manager(tmp_path, models=models, downloader=flaky)
    item.start_batch({m['id']: item.terms_identity(m) for m in models})
    item.batch_thread.join(timeout=5)
    assert item.snapshot()['batch']['state'] == 'failed'
    assert item.snapshot()['batch']['completed'] == ['whisper']
    original = models[1]['terms_url']
    models[1]['terms_url'] = 'https://publisher.invalid/changed'
    with pytest.raises(setup.SetupError, match='terms changed'):
        item.start_batch(resume=True)
    models[1]['terms_url'] = original
    fail[0] = False
    item.start_batch(resume=True)
    item.batch_thread.join(timeout=5)
    assert item.snapshot()['batch']['state'] == 'completed'


def test_batch_cancel_and_duplicate(tmp_path):
    entered = threading.Event()
    def waiting(artifact, destination, *, cancel, progress):
        entered.set()
        cancel.wait(5)
        raise DownloadCancelled()
    models = [entry('whisper'), entry('vision')]
    item = manager(tmp_path, models=models, downloader=waiting)
    selections = {m['id']: item.terms_identity(m) for m in models}
    item.start_batch(selections)
    assert entered.wait(2)
    with pytest.raises(setup.SetupError, match='Another model'):
        manager(tmp_path, models=models).start_batch(selections)
    item.cancel_batch()
    item.batch_thread.join(timeout=5)
    assert item.snapshot()['batch']['state'] == 'cancelled'
    assert not item.target(models[1]).exists()


def test_batch_and_individual_contend_after_locks_acquired(tmp_path):
    models = [entry('whisper'), entry('vision')]
    first = manager(tmp_path, models=models)
    second = manager(tmp_path, models=models)
    selections = {m['id']: first.terms_identity(m) for m in models}
    original = first.active_elsewhere
    checks = [0]
    def interleave(name='setup.lock'):
        if name == 'batch.lock':
            checks[0] += 1
            if checks[0] == 2:  # individual holds setup.lock here
                with pytest.raises(setup.SetupError, match='Another model'):
                    second.start_batch(selections)
        return original(name)
    first.active_elsewhere = interleave
    first.start('whisper', accept_terms=True)
    first.thread.join(timeout=5)
    assert first.readiness(models[0])[1]


def test_batch_cancel_is_refused_during_promotion(tmp_path):
    item = manager(tmp_path)
    original = item.update
    checked = threading.Event()
    def check(state, message, **kwargs):
        if state == 'installing':
            with pytest.raises(setup.SetupError, match='finishing'):
                item.cancel_batch()
            checked.set()
        return original(state, message, **kwargs)
    item.update = check
    model = item.model('vision')
    item.start_batch({'vision': item.terms_identity(model)})
    item.batch_thread.join(timeout=5)
    assert checked.is_set()
    assert item.snapshot()['batch']['state'] == 'completed'


def test_batch_initial_record_write_failure_releases_lock(tmp_path, monkeypatch):
    item = manager(tmp_path)
    model = item.model('vision')
    original = setup.write_json
    def fail_batch(path, value):
        if path.name == 'batch.json':
            raise OSError('disk full')
        return original(path, value)
    monkeypatch.setattr(setup, 'write_json', fail_batch)
    with pytest.raises(OSError, match='disk full'):
        item.start_batch({'vision': item.terms_identity(model)})
    assert not item.active_elsewhere('batch.lock')


def test_batch_final_record_write_failure_releases_lock(tmp_path, monkeypatch):
    item = manager(tmp_path)
    item.work.mkdir(parents=True)
    descriptor = os.open(item.work / 'batch.lock', os.O_RDWR | os.O_CREAT, 0o600)
    setup.fcntl.flock(descriptor, setup.fcntl.LOCK_EX | setup.fcntl.LOCK_NB)
    original = setup.write_json
    def fail_final(path, value):
        if path.name == 'batch.json' and value['state'] == 'completed':
            raise OSError('disk full')
        return original(path, value)
    monkeypatch.setattr(setup, 'write_json', fail_final)
    with pytest.raises(OSError, match='disk full'):
        item.run_batch({'selections': {}, 'state': 'running', 'completed': [], 'current': None}, descriptor)
    assert not item.active_elsewhere('batch.lock')


def test_batch_restart_recovers_interrupted_promotion_and_resumes(tmp_path):
    models = [entry('whisper'), entry('vision')]
    item = manager(tmp_path, models=models)
    model = models[0]
    target = item.target(model)
    target.parent.mkdir(parents=True)
    target.write_bytes(BODY)
    stat = target.stat()
    record = {'manifest_hash': item.manifest_hash(model), 'files': [{'name': 'weights.bin', 'size': len(BODY),
              'mtime_ns': stat.st_mtime_ns, 'sha256': hashlib.sha256(BODY).hexdigest()}]}
    setup.write_json(item.work / 'whisper.promotion.json', record)
    selections = {m['id']: item.terms_identity(m) for m in models}
    setup.write_json(item.work / 'batch.json', {'state': 'running', 'selections': selections,
                                               'completed': [], 'current': 'whisper', 'error': None})
    assert item.snapshot()['batch']['state'] == 'interrupted'
    item.start_batch(resume=True)
    item.batch_thread.join(timeout=5)
    assert item.snapshot()['batch']['state'] == 'completed'
    assert all(row['verified'] for row in item.snapshot()['models'])
