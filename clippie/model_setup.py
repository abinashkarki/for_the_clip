"""Explicit first-run model provisioning, separate from inference/provider calls.

Only the shipped catalog can choose download URLs/paths. A process-wide file
lock serializes setup, while durable progress and partial files survive restart.
Existing model directories are preserved until a complete verified replacement
is ready; a tiny promotion journal allows interrupted replacement to recover.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

from .model_downloads import DownloadCancelled, DownloadError, download_verified
from .paths import resource_root

BUSY = {'downloading', 'verifying', 'converting', 'installing', 'cancelling'}


class SetupError(RuntimeError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def catalog():
    models = json.loads((resource_root() / 'model-catalog.json').read_text())['models']
    for model in models:
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', model['id']) or not re.fullmatch(r'[A-Za-z0-9_.-]+', model['target']):
            raise RuntimeError('Invalid bundled model catalog.')
        for artifact in model['artifacts']:
            name = Path(artifact['name'])
            if name.is_absolute() or '..' in name.parts or not re.fullmatch('[a-f0-9]{64}', artifact['sha256']):
                raise RuntimeError('Invalid bundled model artifact.')
    return models


def file_hash(path, cancel):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            if cancel.is_set():
                raise DownloadCancelled()
            result.update(chunk)
    if cancel.is_set():
        raise DownloadCancelled()
    return result.hexdigest()


def convert_whisper(source, target, cancel):
    # Keep MLX/Torch initialization out of the web server and permit cancellation
    # during native preparation. The child stays in the owned desktop group.
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    process = subprocess.Popen([sys.executable, '-s', '-m', 'clippie.model_setup',
                                '--convert-whisper', str(source), str(target)],
                               env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            if cancel.wait(0.1):
                raise DownloadCancelled()
        if process.returncode != 0:
            raise SetupError('Could not prepare the speech model. Retry or repair the Clippie installation.')
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


class ModelSetup:
    def __init__(self, root, *, models=None, downloader=download_verified, converter=convert_whisper,
                 dependency_check=None):
        self.root = Path(root).resolve()
        self.models = {item['id']: item for item in (catalog() if models is None else models)}
        self.work = self.root / 'models/.setup'
        self.downloader, self.converter = downloader, converter
        self.dependency_check = dependency_check or (lambda names: all(importlib.util.find_spec(name) is not None for name in names))
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.thread = None
        self.state = None
        self.committing = False
        self.last_write = 0.0
        self.batch_thread = None
        self.batch_cancel = threading.Event()
        self.batch_shutdown = False

    def target(self, model):
        return self.root / 'models' / model['target']

    def managed(self, model):
        # Don't overwrite environment-managed models or follow writable-state
        # symlinks outside this app's data root.
        target = self.target(model)
        safe = (not os.getenv(model['override']) and not target.is_symlink() and not self.work.is_symlink()
                and (self.root / 'models').resolve().is_relative_to(self.root)
                and self.work.resolve().is_relative_to(self.root / 'models'))
        if model['layout'] == 'directory':
            safe = safe and all((target / name).resolve().is_relative_to(target.resolve()) for name in model['required_files'])
        return safe

    def validate_worktree(self):
        for directory, dirs, files in os.walk(self.work, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in dirs + files):
                raise SetupError('Model setup contains a linked file or folder. Choose an ordinary app data folder before preparing models.')

    def model(self, ident):
        if ident not in self.models:
            raise SetupError('Unknown local model.', 422)
        return self.models[ident]

    def active_elsewhere(self, name='setup.lock'):
        path = self.work / name
        if not path.exists():
            return False
        try:
            descriptor = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            finally:
                os.close(descriptor)
        except BlockingIOError:
            return True
        except OSError:
            return True  # uncertain ownership is not permission to start work

    def receipt(self, model):
        return self.work / 'receipts' / (model['id'] + '.json')

    def manifest_hash(self, model):
        identity = {key: model.get(key) for key in ('id', 'target', 'layout', 'conversion')}
        identity['required_files'] = sorted(model['required_files'])
        identity['artifacts'] = sorted([{key: item[key] for key in ('name', 'size', 'sha256')} for item in model['artifacts']], key=lambda item: item['name'])
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def compatible_receipt(self, model, receipt):
        if receipt.get('manifest_hash') == self.manifest_hash(model):
            return True
        # Receipts from the initial prototype hashed human-readable labels too.
        # Accept their unchanged pinned artifacts/conversion after cosmetic app
        # updates instead of forcing gigabytes of unnecessary downloads.
        expected = sorted((item['name'], item['size'], item['sha256']) for item in model['artifacts'])
        try:
            stored = sorted((item['name'], item['size'], item['sha256']) for item in receipt['source_artifacts'])
            return expected == stored and receipt.get('conversion') == model.get('conversion')
        except (KeyError, TypeError):
            return False

    def cached_bytes(self, model):
        cached = 0
        for artifact in model['artifacts']:
            path = self.work / model['id'] / 'downloads' / artifact['name']
            if not path.is_file():
                path = Path(str(path) + '.part')
            try:
                cached += min(artifact['size'], path.stat().st_size) if path.is_file() else 0
            except OSError:
                pass
        return cached

    def additional_space(self, model):
        # A resume must not demand the original download budget a second time.
        # Partial/verified cache bytes already occupy disk and are reused. The
        # downloader still verifies them before promotion; corrupt caches fail
        # safely and may need extra space for their preserved quarantine copy.
        return max(16 * 1024 * 1024, model['required_disk_bytes'] - self.cached_bytes(model))

    def readiness(self, model):
        target = self.target(model)
        if not self.managed(model) and os.getenv(model['override']):
            target = Path(os.environ[model['override']]).expanduser()
        paths = {name: target / name if model['layout'] == 'directory' else target for name in model['required_files']}
        try:
            stats = {name: path.stat() for name, path in paths.items() if path.is_file()}
        except OSError:
            return False, False
        ready = len(stats) == len(paths) and all(stat.st_size > 0 for stat in stats.values())
        receipt = read_json(self.receipt(model))
        verified = ready and self.compatible_receipt(model, receipt)
        if verified:
            recorded = {item['name']: item for item in receipt.get('files', [])}
            for name, stat in stats.items():
                item = recorded.get(name, {})
                if (stat.st_size, stat.st_mtime_ns) != (item.get('size'), item.get('mtime_ns')):
                    verified = False
                    break
        if receipt and not verified:
            ready = False  # setup-owned files changed; do not label them ready
        if (self.work / (model['id'] + '.promotion.json')).exists():
            ready = verified = False
        return ready, bool(verified)

    def snapshot(self):
        with self.lock:
            local_active = self.thread is not None and self.thread.is_alive()
            job = dict(self.state if local_active and self.state else read_json(self.work / 'job.json'))
            active = job.get('id') if job.get('state') in BUSY and (local_active or self.active_elsewhere()) else None
            if job.get('state') in BUSY and not active:
                job.update(state='interrupted', message='Preparation stopped when Clippie closed. Resume to keep verified downloads.', error=None)
            rows = []
            for ident, model in self.models.items():
                ready, verified = self.readiness(model)
                row = {key: model[key] for key in ('id', 'label', 'description', 'required', 'source_url', 'terms_url', 'required_disk_bytes')}
                total = sum(item['size'] for item in model['artifacts'])
                row.update(dependencies_installed=self.dependency_check(model['dependencies']), managed=self.managed(model),
                           ready=ready, verified=verified, state='ready' if verified else 'existing' if ready else 'missing',
                           download_bytes=total, remaining_download_bytes=max(0, total - self.cached_bytes(model)), bytes_total=total, bytes_received=0, message='', error=None)
                row['required_disk_bytes'] = self.additional_space(model)
                row['terms_identity'] = self.terms_identity(model)
                if ident == job.get('id') and (job.get('state') != 'ready' or verified):
                    row.update({key: job[key] for key in ('state', 'bytes_received', 'message', 'error') if key in job})
                rows.append(row)
            batch = read_json(self.work / 'batch.json')
            if batch.get('state') == 'running' and not (self.batch_thread and self.batch_thread.is_alive()) and not self.active_elsewhere('batch.lock'):
                batch['state'] = 'interrupted'
            return {'desktop': os.getenv('CLIPPIE_DESKTOP') == '1', 'active': active, 'models': rows, 'batch': batch or None}

    def update(self, state, message, *, received=None, error=None, throttle=False):
        with self.lock:
            if self.cancel_event.is_set() and state in BUSY and not self.committing:
                state, message = 'cancelling', 'Stopping preparation; downloaded bytes will be kept.'
            self.state.update(state=state, message=message, error=error)
            if received is not None:
                self.state['bytes_received'] = received
            stamp = time.monotonic()
            if not throttle or stamp - self.last_write >= 0.5:
                write_json(self.work / 'job.json', self.state)
                self.last_write = stamp

    def start(self, ident, *, accept_terms, batch_owned=False):
        model = self.model(ident)
        if not accept_terms:
            raise SetupError('Review the publisher terms and confirm the model download.', 422)
        with self.lock:
            if batch_owned and self.batch_cancel.is_set():
                raise DownloadCancelled()
            if not batch_owned and ((self.batch_thread and self.batch_thread.is_alive()) or self.active_elsewhere('batch.lock')):
                raise SetupError('A model setup batch is already running.')
            if not self.managed(model):
                raise SetupError('This model location is managed outside app setup.')
            if not self.dependency_check(model['dependencies']):
                raise SetupError('This installation is missing a required dependency. Repair or reinstall Clippie.')
            if self.thread is not None and self.thread.is_alive():
                raise SetupError('Another model is being prepared. Wait or cancel it first.')
            self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.validate_worktree()
            descriptor = os.open(self.work / 'setup.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SetupError('Another model is being prepared. Wait or cancel it first.') from None
                if not batch_owned and self.active_elsewhere('batch.lock'):
                    raise SetupError('A model setup batch is already running.')
                self.recover_promotion(model)
                ready, _ = self.readiness(model)
                if ready:
                    os.close(descriptor)
                    return self.snapshot()  # never silently replace a usable existing model
                payload = self.work / ident / 'payload'
                if payload.exists():
                    shutil.rmtree(payload)  # disposable staging; verified downloads are kept
                if shutil.disk_usage(self.root).free < self.additional_space(model):
                    raise SetupError('Not enough free space to prepare this model. Free space and retry.', 507)
                self.cancel_event = threading.Event()
                self.state = {'id': ident, 'state': 'downloading', 'bytes_received': 0,
                              'message': 'Starting verified model download.', 'error': None}
                write_json(self.work / 'job.json', self.state)
                self.thread = threading.Thread(target=self.prepare, args=(model, descriptor), daemon=True,
                                               name='clippie-model-setup')
                self.thread.start()
            except BaseException:
                os.close(descriptor)
                raise
        return self.snapshot()

    def terms_identity(self, model):
        # Pins the displayed URL and artifact manifest, not the contents of the
        # remote terms page. A publisher can change those contents at the same URL.
        return hashlib.sha256(json.dumps({'manifest': self.manifest_hash(model), 'terms_url': model['terms_url'], 'source_url': model['source_url']}, sort_keys=True).encode()).hexdigest()

    def start_batch(self, selections=None, *, resume=False):
        with self.lock:
            if (self.batch_thread and self.batch_thread.is_alive()) or (self.thread and self.thread.is_alive()) or self.active_elsewhere() or self.active_elsewhere('batch.lock'):
                raise SetupError('Another model preparation is already running.')
            previous = read_json(self.work / 'batch.json')
            if resume:
                if previous.get('state') not in ('running', 'interrupted', 'failed'):
                    raise SetupError('There is no interrupted batch to resume.')
                selections = previous.get('selections', {})
            if not selections or not isinstance(selections, dict):
                raise SetupError('Select models and accept their displayed terms.', 422)
            for ident, identity in selections.items():
                model = self.model(ident)
                if identity != self.terms_identity(model):
                    raise SetupError('Model files or terms changed. Review and accept the current terms.', 409)
                if not self.readiness(model)[0]:
                    if not self.managed(model):
                        raise SetupError(f"{model['label']} is managed outside app setup.")
                    if not self.dependency_check(model['dependencies']):
                        raise SetupError(f"{model['label']} is missing a required runtime. Repair or reinstall Clippie.")
            self.work.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.validate_worktree()
            descriptor = os.open(self.work / 'batch.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                os.close(descriptor)
                raise SetupError('Another model setup batch is already running.') from None
            if self.active_elsewhere():
                os.close(descriptor)
                raise SetupError('Another model preparation is already running.')
            batch = {'state': 'running', 'selections': selections, 'completed': [], 'current': None, 'error': None}
            try:
                write_json(self.work / 'batch.json', batch)
                self.batch_cancel = threading.Event()
                self.batch_shutdown = False
                self.batch_thread = threading.Thread(target=self.run_batch, args=(batch, descriptor), daemon=True, name='clippie-model-batch')
                self.batch_thread.start()
            except BaseException:
                os.close(descriptor)
                raise
        return self.snapshot()

    def run_batch(self, batch, descriptor):
        try:
            for ident in batch['selections']:
                if self.batch_cancel.is_set():
                    batch['state'] = 'interrupted' if self.batch_shutdown else 'cancelled'
                    break
                model = self.model(ident)
                if self.readiness(model)[0]:
                    batch['completed'].append(ident)
                    write_json(self.work / 'batch.json', batch)
                    continue
                if batch['selections'][ident] != self.terms_identity(model):
                    raise SetupError('Model files or terms changed. Review and accept the current terms.')
                batch['current'] = ident
                write_json(self.work / 'batch.json', batch)
                self.start(ident, accept_terms=True, batch_owned=True)
                thread = self.thread
                if thread:
                    thread.join()
                if self.batch_cancel.is_set():
                    batch['state'] = 'interrupted' if self.batch_shutdown else 'cancelled'
                    break
                if not self.readiness(model)[0]:
                    job = read_json(self.work / 'job.json')
                    raise SetupError(job.get('error') or 'Model preparation stopped. Retry remaining models.')
                batch['completed'].append(ident)
                write_json(self.work / 'batch.json', batch)
            else:
                batch['state'] = 'completed'
        except DownloadCancelled:
            batch['state'] = 'interrupted' if self.batch_shutdown else 'cancelled'
        except Exception as error:
            batch['state'] = 'failed'
            batch['error'] = str(error)
        finally:
            try:
                batch['current'] = None
                write_json(self.work / 'batch.json', batch)
            finally:
                os.close(descriptor)

    def cancel_batch(self):
        with self.lock:
            if not self.batch_thread or not self.batch_thread.is_alive():
                raise SetupError('There is no active batch to cancel.')
            if self.committing:
                raise SetupError('Installation is finishing. Wait for it to complete before cancelling.')
            self.batch_cancel.set()
            if self.thread and self.thread.is_alive():
                self.cancel_event.set()
                self.update('cancelling', 'Stopping preparation; downloaded bytes will be kept.')
        return self.snapshot()

    def cancel(self, ident):
        self.model(ident)
        with self.lock:
            if not self.thread or not self.thread.is_alive() or self.state['id'] != ident:
                raise SetupError('This model has no active preparation to cancel.')
            if self.committing:
                raise SetupError('Installation is finishing. Wait for it to complete.')
            self.cancel_event.set()
            self.update('cancelling', 'Stopping preparation; downloaded bytes will be kept.')
        return self.snapshot()

    def recover_promotion(self, model):
        journal = self.work / (model['id'] + '.promotion.json')
        if not journal.exists():
            return
        # All paths derive from the catalog, never from journal JSON.
        target, backup = self.target(model), self.work / (model['id'] + '.previous')
        if backup.exists() and not target.exists():
            os.replace(backup, target)
            journal.unlink()
            return
        record = read_json(journal)
        expected = {item['name']: item for item in record.get('files', [])}
        valid = self.compatible_receipt(model, record)
        for name in model['required_files']:
            path = target / name if model['layout'] == 'directory' else target
            item = expected.get(name, {})
            if not path.is_file() or path.stat().st_size != item.get('size') or file_hash(path, threading.Event()) != item.get('sha256'):
                valid = False
                break
        if valid:
            write_json(self.receipt(model), record)
            write_json(self.work / 'job.json', {'id': model['id'], 'state': 'ready', 'bytes_received': sum(a['size'] for a in model['artifacts']),
                                              'message': 'Verified installation recovered after restart.', 'error': None})
            journal.unlink()
        elif backup.exists() and target.exists():
            os.replace(target, self.work / (model['id'] + '.incomplete-' + str(time.time_ns())))
            os.replace(backup, target)
            journal.unlink()

    def prepare(self, model, descriptor):
        payload = self.work / model['id'] / 'payload'
        try:
            if payload.exists():
                shutil.rmtree(payload)
            payload.mkdir(parents=True, mode=0o700)
            cache = self.work / model['id'] / 'downloads'
            received = 0
            for artifact in model['artifacts']:
                base = received
                source = self.downloader(artifact, cache / artifact['name'], cancel=self.cancel_event,
                                         progress=lambda count, total, base=base: self.update('downloading', 'Downloading model files.', received=base + count, throttle=True))
                received += artifact['size']
                self.update('verifying', 'Checking downloaded model files.', received=received)
                # Download implementations/test transports must obey the same
                # contract; staging is reverified before the model can be ready.
                if source.stat().st_size != artifact['size'] or file_hash(source, self.cancel_event) != artifact['sha256']:
                    raise DownloadError('Model artifact verification failed.')
                if not model.get('conversion'):
                    destination = payload / artifact['name']
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            if model.get('conversion'):
                self.update('converting', 'Preparing the speech model for Apple silicon. This may take a few minutes.')
                self.converter(cache / model['artifacts'][0]['name'], payload, self.cancel_event)
            files = []
            for name in model['required_files']:
                path = payload / name
                if not path.is_file() or path.stat().st_size == 0:
                    raise SetupError('Model preparation did not produce a complete model. Retry or repair Clippie.')
                digest = file_hash(path, self.cancel_event)
                artifact = next((item for item in model['artifacts'] if item['name'] == name), None)
                if artifact and digest != artifact['sha256']:
                    raise DownloadError('Staged model verification failed.')
                stat = path.stat()
                files.append({'name': name, 'sha256': digest, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns})
            with self.lock:
                if self.cancel_event.is_set():
                    raise DownloadCancelled()
                self.committing = True
                self.update('installing', 'Installing the verified model.')
            target = self.target(model)
            backup = self.work / (model['id'] + '.previous')
            journal = self.work / (model['id'] + '.promotion.json')
            record = {'manifest_hash': self.manifest_hash(model), 'files': files, 'verified_at': time.time(),
                      'conversion': model.get('conversion'), 'source_artifacts': model['artifacts']}
            with self.lock:
                write_json(journal, record)
                if target.exists():
                    if backup.exists():
                        raise SetupError('A previous model copy is preserved. Restore it before replacing this model.')
                    os.replace(target, backup)
                promoted = payload if model['layout'] == 'directory' else payload / model['required_files'][0]
                try:
                    os.replace(promoted, target)
                    write_json(self.receipt(model), record)
                except BaseException:
                    if target.exists():
                        if model['layout'] == 'directory':
                            os.replace(target, payload)
                        else:
                            os.replace(target, payload / model['required_files'][0])
                    if backup.exists():
                        os.replace(backup, target)
                    raise
                journal.unlink(missing_ok=True)
            self.update('ready', 'Model prepared and verified. It is ready for offline use.', received=received)
        except DownloadCancelled:
            self.update('cancelled', 'Preparation cancelled. Verified downloads are kept for Resume.')
        except (SetupError, DownloadError) as error:
            self.update('error', 'Model preparation needs attention.', error=str(error))
        except OSError:
            self.update('error', 'Model preparation needs attention.', error='Could not save model files. Check free space and folder permissions, then retry.')
        except Exception:
            self.update('error', 'Model preparation needs attention.', error='Model preparation failed. Retry or repair the Clippie installation.')
        finally:
            with self.lock:
                self.committing = False
            os.close(descriptor)

    def shutdown(self):
        with self.lock:
            self.batch_shutdown = True
            self.batch_cancel.set()
            self.cancel_event.set()
            thread = self.thread
            batch_thread = self.batch_thread
        if thread:
            thread.join(timeout=3)
        if batch_thread:
            batch_thread.join(timeout=3)


_MANAGERS = {}
_REGISTRY_LOCK = threading.Lock()


def manager(root):
    root = str(Path(root).resolve())
    with _REGISTRY_LOCK:
        if root not in _MANAGERS:
            _MANAGERS[root] = ModelSetup(root)
        return _MANAGERS[root]


def shutdown(root):
    with _REGISTRY_LOCK:
        item = _MANAGERS.get(str(Path(root).resolve()))
    if item:
        item.shutdown()


def main():
    parser = argparse.ArgumentParser(description='Local model setup helper')
    parser.add_argument('--convert-whisper', nargs=2, metavar=('CHECKPOINT', 'TARGET'))
    args = parser.parse_args()
    if args.convert_whisper:
        from .local_models import convert_cached_small
        convert_cached_small(*args.convert_whisper)
    else:
        parser.error('Use the Clippie setup screen to prepare models.')


if __name__ == '__main__':
    main()
