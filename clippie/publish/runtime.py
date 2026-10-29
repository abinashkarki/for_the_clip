"""Wiring: store, secrets, HTTP client and per-channel publishers. Tests replace `http_factory` and `sleep`."""
import threading
import time

import httpx

from . import secrets as secret_module
from .aggregator import Aggregator, BASE_URL
from .base import AuthError
from .store import PublishStore
from .youtube import YouTube

SECRETS = secret_module.SecretStore()
_STORES, _LOCK = {}, threading.Lock()


def http_factory():
    return httpx.Client(timeout=httpx.Timeout(30.0, read=120.0, write=300.0), follow_redirects=False)


def sleep(seconds):
    time.sleep(seconds)


def store(root):
    key = str(root)
    with _LOCK:
        if key not in _STORES:
            _STORES[key] = PublishStore(root)
        return _STORES[key]


def reset():
    with _LOCK:
        _STORES.clear()


def refresh_name(channel_id):
    return f'youtube:{channel_id}:refresh'


def youtube_client(http, db, channel=None, **kwargs):
    client_id, client_secret = db.setting('youtube_client_id'), SECRETS.get('youtube_client_secret')
    if not client_id or not client_secret:
        raise AuthError('Add your Google Cloud OAuth client ID and secret on the Channels page first.')
    refresh = SECRETS.get(channel['token_ref']) if channel else None
    if channel and not refresh:
        raise AuthError('This YouTube channel needs to be connected again.')
    return YouTube(http, client_id, client_secret, refresh, sleep=sleep, **kwargs)


def aggregator_client(http, db):
    key = SECRETS.get('aggregator_key')
    if not key:
        raise AuthError('Add your posting-service API key on the Channels page first.')
    return Aggregator(http, key, db.setting('aggregator_base_url') or BASE_URL)


def publisher(http, db, channel, **kwargs):
    if channel['provider'] == 'youtube':
        return youtube_client(http, db, channel, **kwargs)
    return aggregator_client(http, db)
