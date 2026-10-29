"""Cloud control plane API. Per-device bearer tokens, HTTPS assumed (terminate TLS at the proxy)."""
from contextlib import asynccontextmanager
import hmac
import os
import time

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .crypto import Vault
from .poller import OFFSETS, Poller
from .store import Store


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class DeviceBody(Strict):
    name: str = Field(min_length=1, max_length=80)


class AccountBody(Strict):
    platform: str = Field(pattern='^youtube$')
    refresh_token: str = Field(min_length=1, max_length=2000)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret: str = Field(min_length=1, max_length=300)


class PostBody(Strict):
    channel_id: str = Field(min_length=1, max_length=100)
    platform: str = Field(pattern='^youtube$')
    external_id: str = Field(min_length=1, max_length=100)
    published_at: float | None = None
    scheduled_at: float | None = None
    created_at: float | None = None


def default_http():
    return httpx.Client(timeout=httpx.Timeout(30.0))


def create_app(db_path=None, fernet_key=None, admin_key=None, http_factory=default_http, poll_seconds=None,
               require_https=None, now=time.time):
    db_path = db_path or os.environ.get('CLOUD_DB', 'cloud.sqlite3')
    vault = Vault(fernet_key or os.environ.get('CLOUD_FERNET_KEY', ''))
    admin_key = admin_key if admin_key is not None else os.environ.get('CLOUD_ADMIN_KEY', '')
    require_https = require_https if require_https is not None else os.environ.get('CLOUD_REQUIRE_HTTPS') == '1'
    store = Store(db_path)
    poller = Poller(store, vault, http_factory, now)
    interval = poll_seconds if poll_seconds is not None else float(os.environ.get('CLOUD_POLL_SECONDS', '60'))

    @asynccontextmanager
    async def lifespan(_):
        if interval > 0:
            poller.start(interval)
        yield
        poller.shutdown()

    app = FastAPI(title='Clippie cloud', lifespan=lifespan)
    app.state.store, app.state.poller, app.state.vault = store, poller, vault

    @app.middleware('http')
    async def https_only(request: Request, call_next):
        proto = request.headers.get('x-forwarded-proto', request.url.scheme)
        if require_https and proto != 'https' and request.url.path != '/health':
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': 'HTTPS is required.'}, status_code=400)
        return await call_next(request)

    def bearer(authorization):
        scheme, _, token = (authorization or '').partition(' ')
        if scheme.lower() != 'bearer' or not token:
            raise HTTPException(401, 'Missing bearer token.')
        return token

    def device(authorization: str | None = Header(default=None)):
        found = store.device_for(bearer(authorization))
        if found is None:
            raise HTTPException(401, 'Unknown device token.')
        return found

    @app.get('/health')
    def health():
        return {'ok': True}

    @app.post('/devices', status_code=201)
    def register_device(body: DeviceBody, authorization: str | None = Header(default=None)):
        if not admin_key or not hmac.compare_digest(bearer(authorization), admin_key):
            raise HTTPException(401, 'Invalid admin key.')
        identity, token = store.create_device(body.name)
        return {'device_id': identity, 'token': token}

    @app.put('/accounts/{channel_id}')
    def put_account(channel_id: str, body: AccountBody, owner: str = Depends(device)):
        sealed = vault.seal({'refresh_token': body.refresh_token, 'client_id': body.client_id,
                             'client_secret': body.client_secret})
        store.put_account(owner, channel_id, body.platform, sealed)
        return {'ok': True}

    @app.delete('/accounts/{channel_id}', status_code=204)
    def delete_account(channel_id: str, owner: str = Depends(device)):
        store.delete_account(owner, channel_id)

    @app.put('/posts/{post_id}')
    def put_post(post_id: str, body: PostBody, owner: str = Depends(device)):
        if store.get_account(owner, body.channel_id) is None:
            raise HTTPException(422, 'Register the channel before its posts.')
        first = (body.scheduled_at or now()) + 120 if not body.published_at else body.published_at + OFFSETS[0]
        store.put_post(owner, post_id, body.channel_id, body.external_id, body.published_at, body.scheduled_at,
                       body.created_at or now(), first)
        return {'ok': True}

    @app.delete('/posts/{post_id}', status_code=204)
    def delete_post(post_id: str, owner: str = Depends(device)):
        store.delete_post(owner, post_id)

    @app.get('/snapshots')
    def snapshots(since: float = 0.0, owner: str = Depends(device)):
        return {'snapshots': store.snapshots_since(owner, since)}

    return app
