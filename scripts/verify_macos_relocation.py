#!/usr/bin/env python3
"""Relocate the app and deny developer paths/external networking during smoke.

This provides stronger developer-host evidence, not an independent clean-Mac test.
It uses macOS sandbox-exec only as a test harness; the shipped app is not sandboxed
by this profile. Explicit existing weights are APFS-cloned into temporary storage.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/macos-spike/relocation')
    parser.add_argument('--whisper-model', type=Path)
    parser.add_argument('--embedding-model', type=Path)
    parser.add_argument('--speech-fixture', type=Path)
    args = parser.parse_args()
    if not Path('/usr/bin/sandbox-exec').is_file():
        parser.error('This host does not provide the sandbox-exec test harness.')
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # A failed/unfinished rerun must not leave an earlier successful report
    # looking like the outcome of the current verification attempt.
    (destination / 'relocation-report.json').write_text(json.dumps({'passed': False, 'state': 'running', 'clean_mac': False}, indent=2) + '\n')
    denied = [str(ROOT), '/opt/homebrew', '/usr/local', str(Path.home() / 'anaconda3'),
              str(Path.home() / '.cache'), str(Path.home() / '.local/share/uv'),
              str(Path.home() / 'Library/Caches/uv'), str(Path.home() / 'Library/Python'),
              '/Applications/Xcode.app', '/Library/Developer/CommandLineTools',
              '/usr/bin/swift', '/usr/bin/swiftc', '/usr/bin/xcrun',
              '/usr/bin/xcodebuild', '/usr/bin/clang', '/usr/bin/make']
    with tempfile.TemporaryDirectory(prefix='clippie-relocation-') as temporary:
        staging = Path(temporary).resolve()
        app = staging / 'Moved Clippie.app'
        subprocess.run(['/bin/cp', '-cR', str(args.app.resolve(strict=True)), str(app)], check=True)
        (staging / 'scripts').mkdir()
        (staging / 'packaging/macos').mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/smoke_macos.py', staging / 'scripts/smoke_macos.py')
        shutil.copy2(ROOT / 'packaging/macos/probe_runtime.py', staging / 'packaging/macos/probe_runtime.py')
        profile = staging / 'isolation.sb'
        profile.write_text('(version 1)\n(allow default)\n' +
                           '(deny file-read* ' + ' '.join('(subpath ' + json.dumps(path) + ')' for path in denied) + ')\n' +
                           '(deny network-outbound (require-not (remote ip "localhost:*")))\n')
        python = app / 'Contents/Resources/python/bin/python3'
        environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(Path.home()),
                       'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
        # Verify denial is actually enforced before drawing conclusions from it.
        marker = staging / 'verify-denial.py'
        marker.write_text('from pathlib import Path\nimport socket, errno\ntry:\n Path(' + repr(str(ROOT / 'README.md')) + ').read_bytes()\nexcept PermissionError:\n print("developer_source_denied")\nelse:\n raise SystemExit("sandbox did not deny source reads")\n'
                          'try:\n Path("/usr/bin/swiftc").read_bytes()\nexcept PermissionError:\n print("developer_tools_denied")\nelse:\n raise SystemExit("sandbox did not deny developer tools")\n'
                          'sock=socket.socket()\nsock.settimeout(2)\ntry:\n sock.connect(("192.0.2.1",443))\nexcept OSError as error:\n'
                          ' if error.errno not in (errno.EPERM, errno.EACCES): raise\n print("external_network_denied")\nelse:\n raise SystemExit("sandbox did not deny external network")\nfinally:\n sock.close()\n')
        try:
            guard = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), str(python), str(marker)],
                                   cwd=staging, env=environment, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as error:
            (destination / 'isolation-guard.log').write_text((error.stdout or b'').decode(errors='replace') + (error.stderr or b'').decode(errors='replace'))
            raise
        (destination / 'isolation-guard.log').write_text(guard.stdout + guard.stderr)
        guard.check_returncode()
        command = ['/usr/bin/sandbox-exec', '-f', str(profile), str(python), '-s',
                   str(staging / 'scripts/smoke_macos.py'), str(app), '--output', str(staging / 'evidence')]
        for name in ('whisper_model', 'embedding_model', 'speech_fixture'):
            source = getattr(args, name)
            if source:
                target = staging / name
                subprocess.run(['/bin/cp', '-cR', str(source.resolve(strict=True)), str(target)], check=True)
                command.extend(['--' + name.replace('_', '-'), str(target)])
        with (destination / 'isolation.stdout.log').open('w') as stdout, (destination / 'isolation.stderr.log').open('w') as stderr:
            result = subprocess.run(command, cwd=staging, env=environment, stdout=stdout, stderr=stderr, timeout=360)
        evidence = staging / 'evidence'
        if evidence.exists():
            shutil.copytree(evidence, destination / 'smoke', dirs_exist_ok=True)
        report = {'passed': result.returncode == 0, 'state': 'finished', 'denied_paths': denied,
                  'source_denial_verified': True, 'external_network_denied': True,
                  'developer_tool_denial_verified': True,
                  'relocated_app_name': app.name, 'clean_mac': False,
                  'existing_models_supplied': bool(args.whisper_model or args.embedding_model),
                  'temporary_staging_removed_after_check': True}
        (destination / 'relocation-report.json').write_text(json.dumps(report, indent=2) + '\n')
        result.check_returncode()
    print(json.dumps({'passed': True, 'evidence': str(destination), 'clean_mac': False}))


if __name__ == '__main__':
    main()
