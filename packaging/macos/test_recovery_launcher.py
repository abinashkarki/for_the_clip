"""Recovery protocol boundaries exercised with fake bundled backends only.

Native confirmation dialogs are compile/source checked; these tests never drive
restore UI, open user data, use Keychain, or invoke a provider.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from test_launcher import app_bundle, launcher, run_smoke

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit launcher requires macOS")
SOURCE = Path(__file__).with_name("Launcher.swift")


@pytest.mark.parametrize("code", ["data_too_new", "upgrade_interrupted", "backup_failed", "restore_failed"])
def test_recovery_error_smoke_never_enters_maintenance(tmp_path, launcher, code):
    app = app_bundle(tmp_path, launcher, f"""
import json, os, signal, sys, time
assert sys.argv[1:] == ['-s', '-m', 'clippie.desktop']
if os.getpgrp() != os.getpid():
    os.setsid()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print(json.dumps({{'event': 'error', 'code': {code!r}, 'message': 'Safe fixture summary'}}), flush=True)
while True:
    time.sleep(1)
""")
    result = run_smoke(app)
    assert result.returncode == 1
    assert json.loads(result.stdout)["success"] is False
    assert json.loads(result.stdout)["ready"] is False


def test_progress_messages_preserve_smoke_ready_contract(tmp_path, launcher):
    app = app_bundle(tmp_path, launcher, """
import json, os, signal, sys, time
if os.getpgrp() != os.getpid():
    os.setsid()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
for message in ['Checking local data', 'Verifying backup', 'Finishing startup']:
    print(json.dumps({'event':'startup_progress','message':message}), flush=True)
print(json.dumps({'event':'ready','url':'http://127.0.0.1:59213/__desktop/bootstrap#fixture-secret'}), flush=True)
while True:
    time.sleep(1)
""")
    result = run_smoke(app)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "event": "launcher_smoke", "ready": True, "backend_exit": 0, "success": True,
    }
    assert "fixture-secret" not in result.stdout + result.stderr


def test_short_lived_error_output_is_read_before_exit(tmp_path, launcher):
    app = app_bundle(tmp_path, launcher, """
import json
print(json.dumps({'event':'error','code':'upgrade_interrupted','message':'Safe fixture summary'}), flush=True)
""")
    result = run_smoke(app)
    assert result.returncode == 1
    assert json.loads(result.stdout)["ready"] is False


def test_recovery_ui_requires_selection_and_second_confirmation():
    source = SOURCE.read_text()
    assert 'picker.addItem(withTitle: "Choose a snapshot…")' in source
    assert "picker.indexOfSelectedItem > 0" in source
    assert 'confirmation.addButton(withTitle: "Cancel")' in source
    assert 'confirmation.addButton(withTitle: "Restore Snapshot")' in source
    assert "confirmation.runModal() == .alertSecondButtonReturn" in source
    assert 'arguments: ["--restore-backup", id]' in source
    assert "Keychain credentials are not backed up or reverted" in source
    assert "Files referenced outside this folder" in source
    assert "Remote publishing and costs cannot be undone" in source


def test_shutdown_reaps_group_when_worker_keeps_stdout_open(tmp_path, launcher):
    app = app_bundle(tmp_path, launcher, """
import json, os, signal, subprocess, sys, time
from pathlib import Path
if os.getpgrp() != os.getpid():
    os.setsid()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
worker = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
Path(__file__).with_name('worker-pid').write_text(str(worker.pid))
time.sleep(0.1)
print(json.dumps({'event':'ready','url':'http://127.0.0.1:59213/__desktop/bootstrap#fixture-secret'}), flush=True)
while True:
    time.sleep(1)
""")
    result = run_smoke(app)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["success"] is True
    import os
    pid = int((app / "Contents/Resources/worker-pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_maintenance_progress_uses_same_bounded_silence_watchdog():
    source = SOURCE.read_text()
    progress = source.split('if event == "startup_progress", editorURL == nil {', 1)[1].split('\n            }', 1)[0]
    assert "resetStartupTimer()" in progress
    assert '"Recovery in progress…"' in progress
    assert "maintenance == nil" not in progress.split("setStatus", 1)[0]
