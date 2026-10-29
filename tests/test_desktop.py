import json
import socket
import subprocess
import sys

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from clippie import desktop


PORT = 58439
ORIGIN = f'http://127.0.0.1:{PORT}'


@pytest.fixture
def connection():
    async def endpoint(request):
        return JSONResponse({'private': True, 'method': request.method})
    app = Starlette(routes=[Route('/{rest:path}', endpoint, methods=['GET', 'POST', 'PUT', 'DELETE'])])
    session = desktop.DesktopSession(app, PORT)
    with TestClient(session, base_url=ORIGIN) as client:
        yield session, client


def connect(session, client):
    return client.post('/__desktop/exchange', json={'token': session.bootstrap}, headers={'Origin': ORIGIN})


def test_all_real_routes_require_instance_session(connection):
    _, client = connection
    for path in ('/', '/api/health', '/api/settings', '/media/private.mp4', '/static/index.js'):
        response = client.get(path)
        assert response.status_code == 401
        assert 'private' not in response.text


def test_bootstrap_is_public_but_contains_no_secret_and_clears_fragment(connection):
    session, client = connection
    response = client.get('/__desktop/bootstrap')
    assert response.status_code == 200
    assert session.bootstrap not in response.text and session.session not in response.text
    assert 'history.replaceState' in response.text
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert response.headers['referrer-policy'] == 'no-referrer'


def test_exchange_sets_http_only_instance_cookie_and_cannot_be_replayed(connection):
    session, client = connection
    response = connect(session, client)
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie and 'Path=/' in cookie
    assert session.cookie_name in cookie and session.session not in response.text
    assert client.get('/media/private.mp4').json()['private']
    client.cookies.clear()
    assert connect(session, client).status_code == 401


def test_existing_session_can_reopen_editor_after_exchange(connection):
    session, client = connection
    connect(session, client)
    assert connect(session, client).status_code == 200
    assert client.get('/').status_code == 200


def test_expired_and_wrong_bootstrap_never_authenticate(connection):
    session, client = connection
    assert client.post('/__desktop/exchange', json={'token': 'bad'}, headers={'Origin': ORIGIN}).status_code == 401
    assert not session.consumed
    session.clock = lambda: session.deadline
    assert connect(session, client).status_code == 401
    assert not client.cookies


@pytest.mark.parametrize('token', [123, [], {}, None, 'नेपाली'])
def test_malformed_secret_is_rejected_without_server_error(connection, token):
    _, client = connection
    response = client.post('/__desktop/exchange', json={'token': token}, headers={'Origin': ORIGIN})
    assert response.status_code == 401


def test_exchange_rejects_non_json_or_large_body(connection):
    _, client = connection
    assert client.post('/__desktop/exchange', content='secret', headers={'Origin': ORIGIN}).status_code == 415
    assert client.post('/__desktop/exchange', json={'token': 'x' * 5000}, headers={'Origin': ORIGIN}).status_code == 413


def test_host_origin_and_csrf_are_checked_even_with_cookie(connection):
    session, client = connection
    assert connect(session, client).status_code == 200
    for origin in (None, 'null', 'https://evil.example', 'http://localhost:58439', 'http://127.0.0.1:58440'):
        headers = {} if origin is None else {'Origin': origin}
        assert client.post('/api/settings', json={}, headers=headers).status_code == 403
    assert client.post('/api/settings', json={}, headers={'Origin': ORIGIN}).status_code == 200
    assert client.get('/api/health', headers={'Host': '127.0.0.1:58440'}).status_code == 403
    assert client.get('/api/health', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/health', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/docs').status_code == 404


def test_cookie_from_other_instance_is_not_a_session(connection):
    first, client = connection
    connect(first, client)
    second = desktop.DesktopSession(first.app, PORT)
    assert first.cookie_name != second.cookie_name
    with TestClient(second, base_url=ORIGIN) as other:
        other.cookies.update(client.cookies)
        assert other.get('/api/health').status_code == 401


def test_os_lock_is_held_across_processes_and_released_without_unlink(tmp_path):
    code = ('from pathlib import Path; from clippie.desktop import instance_lock; '
            'import sys;\nwith instance_lock(Path(sys.argv[1])): print("owned")')
    with desktop.instance_lock(tmp_path):
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True)
        assert result.returncode != 0 and 'already using this data folder' in result.stderr
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == 'owned'
    assert (tmp_path / '.desktop.lock').exists()


def test_owned_socket_does_not_attach_to_an_existing_port():
    with desktop.owned_socket() as first, desktop.owned_socket() as second:
        assert first.getsockname()[0] == second.getsockname()[0] == '127.0.0.1'
        assert first.getsockname()[1] != second.getsockname()[1]
        with socket.socket() as contender:
            with pytest.raises(OSError):
                contender.bind(first.getsockname())


def test_lock_precedes_application_import_and_cannot_run_loader_twice(tmp_path, monkeypatch):
    def loader():
        with pytest.raises(desktop.InstanceAlreadyRunning):
            with desktop.instance_lock(tmp_path):
                pass
        raise RuntimeError('stop before application import')
    with pytest.raises(RuntimeError, match='stop before application import'):
        desktop.run(tmp_path, loader=loader)
    with desktop.instance_lock(tmp_path):
        with pytest.raises(desktop.InstanceAlreadyRunning):
            desktop.run(tmp_path, loader=lambda: pytest.fail('loader called while another process owns data'))


def test_smoke_requires_explicit_empty_nonproduction_root(tmp_path, monkeypatch):
    monkeypatch.delenv('CLIPPIE_DATA_DIR', raising=False)
    monkeypatch.delenv('CLIPPIE_SMOKE_TEST', raising=False)
    with pytest.raises(SystemExit):
        desktop.main(['--smoke-test'])
    monkeypatch.setenv('CLIPPIE_DATA_DIR', str(tmp_path))
    (tmp_path / 'existing-user-data').write_text('keep')
    with pytest.raises(SystemExit):
        desktop.main(['--smoke-test'])
    assert (tmp_path / 'existing-user-data').read_text() == 'keep'


def test_runtime_cache_and_offline_mode_are_owned_by_desktop_data(tmp_path, monkeypatch):
    import os
    for name in ('CLIPPIE_DATA_DIR', 'CLIPPIE_DESKTOP', 'PYTHONDONTWRITEBYTECODE', 'HF_HUB_OFFLINE',
                 'TRANSFORMERS_OFFLINE', 'TOKENIZERS_PARALLELISM', 'XDG_CACHE_HOME', 'HF_HOME', 'TORCH_HOME'):
        monkeypatch.setenv(name, 'foreign-value')
    desktop.configure_environment(tmp_path)
    assert os.environ['CLIPPIE_DESKTOP'] == '1'
    assert os.environ['HF_HUB_OFFLINE'] == os.environ['TRANSFORMERS_OFFLINE'] == '1'
    for name in ('CLIPPIE_DATA_DIR', 'XDG_CACHE_HOME', 'HF_HOME', 'TORCH_HOME'):
        assert os.environ[name].startswith(str(tmp_path))


def test_ready_message_follows_successful_startup_and_has_only_bootstrap_url(monkeypatch, capsys):
    import asyncio
    session = desktop.DesktopSession(lambda *_: None, PORT)
    server = desktop.DesktopServer(None, session)

    async def startup(self, sockets=None):
        self.started = True
    monkeypatch.setattr(desktop.uvicorn.Server, 'startup', startup)
    asyncio.run(server.startup())
    ready = json.loads(capsys.readouterr().out)
    assert ready == {'event': 'ready', 'url': session.launch_url}
    assert session.session not in ready['url']
