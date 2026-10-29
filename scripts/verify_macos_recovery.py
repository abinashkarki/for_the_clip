#!/usr/bin/env python3
"""Offline packaged-runtime upgrade/restore drill on synthetic data only.

The six fixture databases test storage mechanics, not real project migration or
the native recovery dialogs. Fresh application startup is checked separately by
smoke_macos.py. No credentials, production data or network calls are used.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from smoke_macos import environment


FIXTURE = r'''
import json, sqlite3, sys
from contextlib import closing
from pathlib import Path
from clippie import desktop, storage_safety as safety
root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
for name in safety.KNOWN_DATABASES:
    with closing(sqlite3.connect(root / name)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE fixture(value TEXT)')
        db.execute('INSERT INTO fixture VALUES (?)', (name,))
        db.commit()
for name, content in {'uploads/source.mp4': b'original-media',
    'models/weights.bin': b'original-model', 'model-calls/blob.json.gz': b'evidence',
    'studio/brand-assets/logo.png': b'logo', 'model-routing.json': b'{}'}.items():
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
with desktop.instance_lock(root):
    pending = safety.prepare_startup(root, build_id='packaged-fixture-v1')
    assert pending['backup_id'] and pending['status'] == 'starting'
    try:
        safety.prepare_startup(root, build_id='packaged-fixture-v1')
    except safety.DataSafetyError as error:
        assert error.code == 'upgrade_interrupted'
    else:
        raise AssertionError('interrupted startup was not refused')
    safety.prepare_startup(root, build_id='packaged-fixture-v1', retry=True)
    safety.complete_startup(root)
    assert safety.version(root)['status'] == 'ready'
    for name in safety.KNOWN_DATABASES:
        with closing(sqlite3.connect(root / name)) as db:
            db.execute('UPDATE fixture SET value=?', ('latest edits',))
            db.commit()
    (root / 'uploads/source.mp4').write_bytes(b'latest-media')
    (root / 'new-file').write_bytes(b'preserve-me')
    print(json.dumps({'backup_id': pending['backup_id']}))
'''

VERIFY = r'''
import json, sqlite3, sys
from contextlib import closing
from pathlib import Path
from clippie import storage_safety as safety
root = Path(sys.argv[1])
rescue_id = sys.argv[2]
for name in safety.KNOWN_DATABASES:
    with closing(sqlite3.connect(root / name)) as db:
        assert db.execute('SELECT value FROM fixture').fetchone()[0] == name
assert (root / 'uploads/source.mp4').read_bytes() == b'original-media'
assert (root / 'models/weights.bin').read_bytes() == b'original-model'
assert (root / 'model-calls/blob.json.gz').read_bytes() == b'evidence'
assert not (root / 'new-file').exists()
assert safety.background_resume_disabled(root)
rescue, record = safety.checked_snapshot(root, rescue_id)
assert (rescue / 'payload/uploads/source.mp4').read_bytes() == b'latest-media'
assert (rescue / 'payload/new-file').read_bytes() == b'preserve-me'
print(json.dumps({'six_databases_restored': True, 'media_models_evidence_restored': True,
    'latest_state_rescue_verified': True, 'background_resume_disabled': True}))
'''

FUTURE = r'''
import sys
from pathlib import Path
from clippie import storage_safety as safety
root = Path(sys.argv[1])
safety.atomic_json(root / safety.VERSION_FILE, {'format_version': 999, 'minimum_reader': 999,
    'root_key': safety.root_key(root), 'status': 'ready', 'build_id': 'future'})
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    app = args.app.resolve(strict=True)
    python = app / 'Contents/Resources/python/bin/python3'
    with tempfile.TemporaryDirectory(prefix='clippie-recovery-drill-') as temporary:
        root = Path(temporary) / 'data'
        env = environment(app, root)
        # Maintenance intentionally opens a populated synthetic root. The smoke
        # flag enforces a fresh-empty-root guard and is inappropriate here.
        env.pop('CLIPPIE_SMOKE_TEST', None)
        def invoke(arguments, expected=0):
            result = subprocess.run([str(python), '-s', *arguments], env=env,
                                    cwd=temporary, capture_output=True, text=True, timeout=120)
            if result.returncode != expected:
                raise RuntimeError('Packaged recovery drill failed: ' + result.stderr[-2000:] + result.stdout[-2000:])
            return [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        fixture = invoke(['-c', FIXTURE, str(root)])[-1]
        info = invoke(['-m', 'clippie.desktop', '--recovery-info'])[-1]
        assert info['event'] == 'recovery_info' and info['backups'][0]['id'] == fixture['backup_id']
        restored = invoke(['-m', 'clippie.desktop', '--restore-backup', fixture['backup_id']])[-1]
        assert restored['event'] == 'restored'
        report = invoke(['-c', VERIFY, str(root), restored['rescue_id']])[-1]
        invoke(['-c', FUTURE, str(root)])
        unchanged = {path.name: path.read_bytes() for path in root.glob('*.sqlite3')}
        refused = invoke(['-m', 'clippie.desktop'], expected=1)[-1]
        assert refused['code'] == 'data_too_new'
        assert unchanged == {path.name: path.read_bytes() for path in root.glob('*.sqlite3')}
        report.update(packaged_cli_restore=True, explicit_interrupted_retry=True,
                      future_format_refused_without_database_change=True,
                      native_dialogs_exercised=False, real_project_migration=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
