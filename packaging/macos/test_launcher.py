"""Compiled launcher contract tests; these are not clean-Mac/backend acceptance."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit launcher requires macOS")


@pytest.fixture(scope="module")
def launcher(tmp_path_factory):
    target = tmp_path_factory.mktemp("launcher-build") / "Clippie"
    subprocess.run(
        ["xcrun", "swiftc", "-O", "-framework", "AppKit", str(ROOT / "packaging/macos/Launcher.swift"), "-o", str(target)],
        check=True, capture_output=True, text=True,
    )
    return target


def app_bundle(tmp_path, launcher, backend):
    app = tmp_path / "Relocated Clippie.app"
    contents = app / "Contents"
    resources = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True)
    shutil.copy2(launcher, contents / "MacOS/Clippie")
    shutil.copy2(ROOT / "packaging/macos/Info.plist", contents / "Info.plist")
    (resources / "app/clippie").mkdir(parents=True)
    (resources / "app/clippie/desktop.py").touch()
    (resources / "python/bin").mkdir(parents=True)
    (resources / "bin").mkdir()
    for name in ("ffmpeg", "ffprobe"):
        tool = resources / "bin" / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    script = resources / "fixture_backend.py"
    script.write_text(backend)
    # The fake interpreter is only a protocol fixture; the actual bundled-runtime
    # integration is covered by the packaging smoke command, not this test.
    executable = resources / "python/bin/python3"
    executable.write_text(
        f"#!{sys.executable}\nimport runpy, sys, traceback\n"
        f"sys.excepthook = lambda *args: open({str(resources / 'fixture-error')!r}, 'w').write(''.join(traceback.format_exception(*args)))\n"
        f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
    )
    executable.chmod(0o755)
    return app


def run_smoke(app):
    environment = dict(os.environ)
    environment.update({"GEMINI_API_KEY": "fixture-secret", "PYTHONPATH": "/fixture-bad-path",
                        "CLIPPIE_DATA_DIR": "/fixture-bad-data", "CLIPPIE_PUBLISH_TOKEN": "fixture-token"})
    return subprocess.run([str(app / "Contents/MacOS/Clippie"), "--smoke-test"],
                          capture_output=True, text=True, env=environment, timeout=20)


def test_readiness_is_owned_and_environment_is_clean(tmp_path, launcher):
    backend = """
import json, os, signal, sys, time
from pathlib import Path
if os.getpgrp() != os.getpid():
    os.setsid()
root = Path(__file__).parent
data = Path(os.environ['CLIPPIE_DATA_DIR'])
assert 'GEMINI_API_KEY' not in os.environ
assert 'CLIPPIE_PUBLISH_TOKEN' not in os.environ
assert os.environ['PYTHONPATH'] == str(root / 'app')
assert os.environ['CLIPPIE_RESOURCE_DIR'] == str(root / 'app/clippie')
assert os.environ['CLIPPIE_SMOKE_TEST'] == '1'
assert data.name.startswith('clippie-launcher-')
assert os.environ['PATH'].split(':')[0] == str(root / 'bin')
assert os.environ['CLIPPIE_FFMPEG'] == str(root / 'bin/ffmpeg')
assert int(os.environ['CLIPPIE_LAUNCHER_PID']) == os.getppid()
(root / 'observed-data-path').write_text(str(data))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print(json.dumps({'event': 'ready', 'url': 'http://127.0.0.1:59213/__desktop/bootstrap#fixture-secret'}), flush=True)
while True:
    time.sleep(1)
"""
    app = app_bundle(tmp_path, launcher, backend)
    result = run_smoke(app)
    errors = app / "Contents/Resources/fixture-error"
    assert result.returncode == 0, errors.read_text() if errors.exists() else result.stderr
    report = json.loads(result.stdout)
    assert report == {"event": "launcher_smoke", "ready": True, "backend_exit": 0, "success": True}
    assert "fixture-secret" not in result.stdout + result.stderr
    data = Path((app / "Contents/Resources/observed-data-path").read_text())
    assert not data.exists(), "smoke must clean its isolated data directory"


def test_missing_bundled_tool_fails_without_starting_backend(tmp_path, launcher):
    app = app_bundle(tmp_path, launcher, "raise AssertionError('backend must not start')\n")
    (app / "Contents/Resources/bin/ffprobe").unlink()
    result = run_smoke(app)
    assert result.returncode == 1
    assert json.loads(result.stdout)["ready"] is False
    assert json.loads(result.stdout)["success"] is False


def test_backend_startup_error_fails_without_opening_browser(tmp_path, launcher):
    app = app_bundle(tmp_path, launcher, """
import json, os, signal, sys, time
if os.getpgrp() != os.getpid():
    os.setsid()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print(json.dumps({'event':'error','message':'Fixture startup failure'}), flush=True)
while True:
    time.sleep(1)
""")
    result = run_smoke(app)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ready"] is False
    assert report["success"] is False


def test_app_manifest_matches_launcher_contract():
    with (ROOT / "packaging/macos/Info.plist").open("rb") as stream:
        manifest = plistlib.load(stream)
    assert manifest["CFBundleExecutable"] == "Clippie"
    assert manifest["NSPrincipalClass"] == "NSApplication"
    assert manifest["CFBundlePackageType"] == "APPL"
