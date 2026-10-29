"""Owned local server for the Mac launcher; source CLI behavior is unchanged.

The launcher owns this process group. The data lock is acquired before importing
the application, whose module-level stores may recover existing work. Browser
access uses a short-lived, single-use bootstrap and an instance-specific cookie.
This boundary does not defend against malware running as the same macOS user.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import importlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import threading
import time

import uvicorn
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from . import storage_safety


class InstanceAlreadyRunning(RuntimeError):
    pass


def same_secret(candidate, expected):
    return isinstance(candidate, str) and secrets.compare_digest(candidate.encode('utf-8'), expected.encode('utf-8'))


@contextmanager
def instance_lock(root: Path):
    """Keep the descriptor alive for ownership; never unlink an active inode."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(root / '.desktop.lock', flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstanceAlreadyRunning('Clippie is already using this data folder. Open the running app or quit it first.') from error
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode('ascii'))
        yield
    finally:
        os.close(descriptor)


def owned_socket():
    """Bind once and pass this same socket to Uvicorn; there is no port race."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', 0))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


BOOTSTRAP_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Opening Clippie</title></head><body><p id="status">Opening Clippie…</p>
<script>
const token = location.hash.slice(1);
history.replaceState(null, '', location.pathname);
fetch('/__desktop/exchange', {
  method: 'POST', credentials: 'same-origin',
  headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})
}).then(response => {
  if (!response.ok) throw new Error('Open Clippie again from the app.');
  location.replace('/');
}).catch(() => {document.getElementById('status').textContent =
  'This opening link expired. Quit and reopen Clippie to connect.';});
</script></body></html>"""


class DesktopSession:
    """ASGI boundary around the existing app, including media and static files."""

    def __init__(self, app, port: int, *, clock=time.monotonic, bootstrap_ttl=120):
        self.app = app
        self.host = f'127.0.0.1:{port}'
        self.origin = f'http://{self.host}'
        self.cookie_name = 'clippie_desktop_' + secrets.token_hex(12)
        self.session = secrets.token_urlsafe(32)
        self.bootstrap = secrets.token_urlsafe(32)
        self.clock = clock
        self.deadline = clock() + bootstrap_ttl
        self.consumed = False

    @property
    def launch_url(self):
        return f'{self.origin}/__desktop/bootstrap#{self.bootstrap}'

    def authenticated(self, request):
        candidate = request.cookies.get(self.cookie_name, '')
        return same_secret(candidate, self.session)

    async def exchange(self, request):
        if request.method != 'POST':
            return JSONResponse({'detail': 'Method not allowed.'}, status_code=405)
        if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
            return JSONResponse({'detail': 'Expected JSON.'}, status_code=415)
        if self.authenticated(request):
            return JSONResponse({'connected': True})
        if self.consumed or self.clock() >= self.deadline:
            return JSONResponse({'detail': 'Opening link expired. Reopen Clippie.'}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({'detail': 'Opening request too large.'}, status_code=413)
        try:
            candidate = json.loads(body).get('token', '')
        except (ValueError, AttributeError):
            candidate = ''
        if not same_secret(candidate, self.bootstrap):
            return JSONResponse({'detail': 'Invalid opening link.'}, status_code=401)
        # No awaits between validation and consumption: concurrent exchanges
        # cannot mint two independent sessions from one opening link.
        self.consumed = True
        response = JSONResponse({'connected': True})
        response.set_cookie(self.cookie_name, self.session, httponly=True,
                            samesite='strict', path='/')
        return response

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)
        if scope['type'] != 'http':
            # The existing editor uses HTTP. Don't expose an unauthenticated
            # websocket path if one is introduced behind this wrapper later.
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 1008})
            return
        request = Request(scope, receive)
        response = None
        if request.headers.get('host') != self.host:
            response = JSONResponse({'detail': 'Wrong app instance.'}, status_code=403)
        elif request.headers.get('origin') not in (None, self.origin):
            response = JSONResponse({'detail': 'Cross-origin requests are disabled.'}, status_code=403)
        elif request.headers.get('sec-fetch-site') == 'cross-site':
            response = JSONResponse({'detail': 'Cross-site requests are disabled.'}, status_code=403)
        elif request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.headers.get('origin') != self.origin:
            response = JSONResponse({'detail': 'An app-origin mutation is required.'}, status_code=403)
        elif request.url.path == '/__desktop/bootstrap' and request.method == 'GET':
            response = HTMLResponse(BOOTSTRAP_PAGE)
            response.headers['Content-Security-Policy'] = (
                "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        elif request.url.path == '/__desktop/exchange':
            response = await self.exchange(request)
        elif not self.authenticated(request):
            response = JSONResponse({'detail': 'Open the editor from the Clippie app.'}, status_code=401)
        elif request.url.path in {'/docs', '/redoc', '/openapi.json'}:
            response = JSONResponse({'detail': 'Not found.'}, status_code=404)

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                existing = {name.lower() for name, _ in headers}
                additions = {
                    b'x-frame-options': b'DENY',
                    b'referrer-policy': b'no-referrer',
                    b'x-content-type-options': b'nosniff',
                    b'cache-control': b'no-store',
                    b'content-security-policy': b"frame-ancestors 'none'; base-uri 'self'",
                }
                headers.extend((name, value) for name, value in additions.items() if name not in existing)
                message = {**message, 'headers': headers}
            await send(message)

        if response is not None:
            await response(scope, receive, secure_send)
        else:
            await self.app(scope, receive, secure_send)


class DesktopServer(uvicorn.Server):
    def __init__(self, config, session, launcher_pid=None):
        super().__init__(config)
        self.session = session
        self.launcher_pid = launcher_pid
        self.launcher_watch = None

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            print(json.dumps({'event': 'ready', 'url': self.session.launch_url}), flush=True)
            if self.launcher_pid is not None:
                self.launcher_watch = asyncio.create_task(self.watch_launcher())
        elif self.should_exit:
            print(json.dumps({'event': 'error', 'code': 'upgrade_interrupted',
                              'message': 'Clippie initialization did not finish. Retry this app or open Recovery.'}), flush=True)

    async def watch_launcher(self):
        while True:
            if os.getppid() != self.launcher_pid:
                self.should_exit = True
                # A killed launcher cannot finish its own shutdown timer. An
                # independent daemon timer covers workers that block exit.
                timer = threading.Timer(5, os.killpg, (os.getpgrp(), signal.SIGKILL))
                timer.daemon = True
                timer.start()
                os.killpg(os.getpgrp(), signal.SIGTERM)
                return
            await asyncio.sleep(0.5)

    async def shutdown(self, sockets=None):
        if self.launcher_watch is not None:
            self.launcher_watch.cancel()
        await super().shutdown(sockets=sockets)


def load_application():
    return importlib.import_module('clippie.app').app


def progress_reporter():
    last = 0.0
    def report(message):
        nonlocal last
        stamp = time.monotonic()
        if stamp - last >= 0.5:
            print(json.dumps({'event': 'startup_progress', 'message': message}), flush=True)
            last = stamp
    return report


def run(root: Path, *, launcher_pid=None, loader=load_application, retry=False):
    """Lock, bind, import, then serve. The sequence prevents recovery races."""
    with instance_lock(root):
        storage_safety.prepare_startup(root, retry=retry, progress=progress_reporter())
        with owned_socket() as sock:
            application = loader()
            session = DesktopSession(application, sock.getsockname()[1])
            config = uvicorn.Config(session, host='127.0.0.1', port=sock.getsockname()[1],
                                    log_level='warning', access_log=False, timeout_graceful_shutdown=4)
            DesktopServer(config, session, launcher_pid).run(sockets=[sock])


def configure_environment(root: Path):
    os.environ['CLIPPIE_DATA_DIR'] = str(root)
    os.environ['CLIPPIE_DESKTOP'] = '1'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    # Local runtimes must consume explicitly prepared models, never fill a
    # developer cache or download/substitute a model during normal inference.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'
    os.environ['XDG_CACHE_HOME'] = str(root / 'cache')
    os.environ['HF_HOME'] = str(root / 'cache/huggingface')
    os.environ['TORCH_HOME'] = str(root / 'cache/torch')
    os.environ['NUMBA_CACHE_DIR'] = str(root / 'cache/numba')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Owned local backend for Clippie.app')
    parser.add_argument('--smoke-test', action='store_true', help='Use an explicit empty test data folder; disable credentials and background recovery')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--recovery-info', action='store_true')
    mode.add_argument('--restore-backup', metavar='ID')
    mode.add_argument('--retry-upgrade', action='store_true')
    args = parser.parse_args(argv)
    smoke = args.smoke_test or os.environ.get('CLIPPIE_SMOKE_TEST') == '1'
    raw_root = os.environ.get('CLIPPIE_DATA_DIR')
    root = Path(raw_root).expanduser().resolve() if raw_root else Path.home() / 'Library/Application Support/Clippie'
    if smoke:
        default_root = Path.home() / 'Library/Application Support/Clippie'
        if not raw_root or root == default_root or (root.exists() and any(root.iterdir())):
            parser.error('Smoke testing requires CLIPPIE_DATA_DIR pointing to a fresh empty test folder.')
        os.environ['CLIPPIE_SMOKE_TEST'] = '1'
    configure_environment(root)
    # A launcher-spawned child normally inherits its parent's group. If a shell
    # already assigned this process its own group, setsid would raise EPERM.
    if os.getpgrp() != os.getpid():
        os.setsid()
    try:
        raw_pid = os.environ.get('CLIPPIE_LAUNCHER_PID')
        launcher_pid = int(raw_pid) if raw_pid else None
        if launcher_pid is not None and launcher_pid != os.getppid():
            raise RuntimeError('The desktop launcher no longer owns this backend.')
        if launcher_pid is not None:
            # Cover backup/restore time before Uvicorn's async watcher exists.
            def watch_parent():
                while os.getppid() == launcher_pid:
                    time.sleep(0.5)
                timer = threading.Timer(5, os.killpg, (os.getpgrp(), signal.SIGKILL))
                timer.daemon = True
                timer.start()
                os.killpg(os.getpgrp(), signal.SIGTERM)
            threading.Thread(target=watch_parent, daemon=True, name='clippie-launcher-owner').start()
        if args.recovery_info or args.restore_backup:
            with instance_lock(root):
                result = (storage_safety.recovery_info(root) if args.recovery_info else
                          storage_safety.restore(root, args.restore_backup, progress=progress_reporter()))
                print(json.dumps(result), flush=True)
        else:
            run(root, launcher_pid=launcher_pid, retry=args.retry_upgrade)
    except storage_safety.DataSafetyError as error:
        print(json.dumps({'event': 'error', 'code': error.code, 'message': str(error)}), flush=True)
        return 1
    except (InstanceAlreadyRunning, OSError, RuntimeError, ValueError) as error:
        # The launcher consumes stdout protocol messages. Do not serialize an
        # arbitrary dependency exception: it may include keys or user content.
        message = (str(error) if isinstance(error, InstanceAlreadyRunning) else
                   'Clippie could not start its local backend. Check the app installation and data-folder access, then reopen it.')
        print(json.dumps({'event': 'error', 'message': message}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
