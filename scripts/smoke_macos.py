#!/usr/bin/env python3
"""Run isolated launcher/native-import/export checks on a built app.

This is a restricted-environment developer-host check, not a clean-Mac claim.
Optional local weights/fixtures must already exist; this script downloads none.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

ROOT = Path(__file__).resolve().parents[1]


def environment(app, data):
    resources = app / 'Contents/Resources'
    allowed = ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', '__CF_USER_TEXT_ENCODING')
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env.update({
        'PATH': str(resources / 'bin') + ':/usr/bin:/bin:/usr/sbin:/sbin',
        'PYTHONPATH': str(resources / 'app'), 'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
        'CLIPPIE_RESOURCE_DIR': str(resources / 'app/clippie'), 'CLIPPIE_DATA_DIR': str(data),
        'CLIPPIE_FFMPEG': str(resources / 'bin/ffmpeg'), 'CLIPPIE_FFPROBE': str(resources / 'bin/ffprobe'),
        'CLIPPIE_DESKTOP': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'HF_HOME': str(data / 'cache/huggingface'), 'XDG_CACHE_HOME': str(data / 'cache'),
        'NUMBA_CACHE_DIR': str(data / 'cache/numba'), 'CLIPPIE_SMOKE_TEST': '1',
    })
    return env


def check_backend(app, data):
    """Exercise the real packaged app/session, never log its bootstrap secret."""
    env = environment(app, data)
    env['CLIPPIE_LAUNCHER_PID'] = str(os.getpid())
    python = app / 'Contents/Resources/python/bin/python3'
    process = subprocess.Popen([str(python), '-s', '-m', 'clippie.desktop'], cwd=data.parent,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 60
            event = None
            while time.monotonic() < deadline:
                if not selector.select(timeout=1):
                    if process.poll() is not None:
                        break
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('event') == 'error':
                    raise RuntimeError('Packaged backend startup failed.')
                if message.get('event') == 'ready':
                    event = message
                    break
            if event is None:
                raise RuntimeError('Packaged backend did not become ready.')
        address, token = event['url'].split('#', 1)
        origin = address.removesuffix('/__desktop/bootstrap')
        try:
            urllib.request.urlopen(origin + '/api/health', timeout=5)
            raise RuntimeError('Private API accepted unauthenticated request.')
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        request = urllib.request.Request(origin + '/__desktop/exchange',
                                         data=json.dumps({'token': token}).encode(),
                                         headers={'Origin': origin, 'Content-Type': 'application/json'})
        with opener.open(request, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError('Desktop bootstrap failed.')
        with opener.open(origin + '/', timeout=5) as response:
            html = response.read().decode()
        asset = re.search(r'(?:src|href)="([^\"]+\.js)"', html)
        if asset is None:
            raise RuntimeError('Built editor asset missing.')
        with opener.open(urllib.parse.urljoin(origin, asset[1]), timeout=5) as response:
            if not response.read():
                raise RuntimeError('Bundled JavaScript is empty.')
        with opener.open(origin + '/api/health', timeout=15) as response:
            health = json.load(response)
        with opener.open(origin + '/api/setup/models', timeout=5) as response:
            setup = json.load(response)
        if {model['id'] for model in setup['models']} != {'whisper', 'embeddings', 'vision'} or setup['active'] is not None:
            raise RuntimeError('Fresh packaged model setup is incomplete or started work automatically.')
        rejected = urllib.request.Request(origin + '/api/setup/models/whisper/install',
                                         data=b'{"accept_terms":false}',
                                         headers={'Origin': origin, 'Content-Type': 'application/json'})
        try:
            opener.open(rejected, timeout=5)
            raise RuntimeError('Model installation started without download consent.')
        except urllib.error.HTTPError as error:
            if error.code != 422:
                raise
        if not health['ffmpeg'] or not health['ffprobe'] or any(health.get(f'{name}_configured') for name in ('gemini', 'anthropic', 'openai', 'groq', 'deepseek', 'nvidia', 'openrouter')):
            raise RuntimeError('Smoke health did not use bundled tools and empty credentials.')
        try:
            opener.open(urllib.request.Request(origin + '/api/health', headers={'Origin': 'https://foreign.invalid'}), timeout=5)
            raise RuntimeError('Foreign-origin request was accepted.')
        except urllib.error.HTTPError as error:
            if error.code != 403:
                raise
        return {'authenticated_editor': True, 'bundled_javascript': True,
                'unauthenticated_api_rejected': True, 'foreign_origin_rejected': True,
                'bundled_ffmpeg': True, 'provider_credentials_empty': True,
                'setup_catalog_available': True, 'setup_requires_consent': True,
                'caption_overlay_available': health['caption_burn_available']}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                raise RuntimeError('Packaged backend required forced termination.')
        process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/macos-spike/smoke')
    parser.add_argument('--whisper-model', type=Path)
    parser.add_argument('--embedding-model', type=Path)
    parser.add_argument('--speech-fixture', type=Path)
    args = parser.parse_args()
    app, output = args.app.resolve(strict=True), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='clippie-runtime-check-') as temp:
        env = environment(app, Path(temp))
        # The launcher independently creates/cleans its own fresh smoke folder.
        launched = subprocess.run([str(app / 'Contents/MacOS/Clippie'), '--smoke-test'], cwd=temp,
                                  env=env, capture_output=True, text=True, timeout=150)
        (output / 'launcher.stdout.log').write_text(launched.stdout)
        (output / 'launcher.stderr.log').write_text(launched.stderr)
        launched.check_returncode()
        events = [json.loads(line) for line in launched.stdout.splitlines() if line.startswith('{')]
        if not any(event.get('event') == 'launcher_smoke' and event.get('ready') for event in events):
            raise RuntimeError('Launcher did not confirm ready and bounded shutdown.')
        backend = check_backend(app, Path(temp) / 'backend')
        (output / 'backend-report.json').write_text(json.dumps(backend, indent=2) + '\n')
        command = [str(app / 'Contents/Resources/python/bin/python3'), '-s',
                   str(ROOT / 'packaging/macos/probe_runtime.py'), '--output', str(output)]
        for option in ('whisper_model', 'embedding_model', 'speech_fixture'):
            value = getattr(args, option)
            if value:
                command.extend(['--' + option.replace('_', '-'), str(value.resolve(strict=True))])
        with (output / 'probe.stdout.log').open('w') as stdout, (output / 'probe.stderr.log').open('w') as stderr:
            subprocess.run(command, cwd=temp, env=env, stdout=stdout, stderr=stderr, check=True, timeout=240)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print(json.dumps({'passed': True, 'app': str(app), 'evidence': str(output),
                      'clean_mac': False, 'model_provisioning': False, 'notarization': False}))


if __name__ == '__main__':
    main()
