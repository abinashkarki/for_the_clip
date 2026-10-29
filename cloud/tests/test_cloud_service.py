"""Cloud control plane: device auth, encryption at rest, poll schedule, snapshots feed. No network."""
import sqlite3

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx
import pytest

from clippie_cloud.app import create_app
from clippie_cloud.crypto import Vault, VaultError
from clippie_cloud.poller import OFFSETS

KEY = Fernet.generate_key().decode()
ROWS = [[0.0, 1.0, 0.5], [0.5, 0.7, 0.4], [1.0, 0.4, 0.3]]


class Google:
    def __init__(self):
        self.privacy, self.publish_at, self.refresh_status, self.requests = 'public', None, 200, []

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.requests.append(request)
        if request.url.path == '/token':
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={'error': 'invalid_grant'})
            return httpx.Response(200, json={'access_token': 'at-1'})
        assert request.headers['authorization'] == 'Bearer at-1'
        if request.url.path == '/youtube/v3/videos':
            status = {'privacyStatus': self.privacy, **({'publishAt': self.publish_at} if self.publish_at else {})}
            return httpx.Response(200, json={'items': [{'statistics': {'viewCount': '50', 'likeCount': '5'}, 'status': status}]})
        if request.url.params.get('dimensions'):
            return httpx.Response(200, json={'columnHeaders': [{'name': 'elapsedVideoTimeRatio'}, {'name': 'audienceWatchRatio'},
                                                               {'name': 'relativeRetentionPerformance'}], 'rows': ROWS})
        names = request.url.params['metrics'].split(',')
        return httpx.Response(200, json={'columnHeaders': [{'name': n} for n in names], 'rows': [[7 for _ in names]]})


@pytest.fixture
def cloud(tmp_path):
    google, clock = Google(), [1_000_000.0]
    app = create_app(tmp_path / 'cloud.sqlite3', KEY, 'admin-secret', google.client, poll_seconds=0, now=lambda: clock[0])
    with TestClient(app) as client:
        client.google, client.clock, client.app_ref, client.db_path = google, clock, app, tmp_path / 'cloud.sqlite3'
        yield client


def enroll(cloud, name='laptop'):
    reply = cloud.post('/devices', json={'name': name}, headers={'Authorization': 'Bearer admin-secret'})
    assert reply.status_code == 201
    return {'Authorization': 'Bearer ' + reply.json()['token']}


def account(cloud, headers, channel='ch1'):
    return cloud.put(f'/accounts/{channel}', headers=headers, json={
        'platform': 'youtube', 'refresh_token': 'refresh-plain-123', 'client_id': 'cid', 'client_secret': 'csecret-plain'})


def track(cloud, headers, post='p1', **fields):
    body = {'channel_id': 'ch1', 'platform': 'youtube', 'external_id': 'vid1', 'published_at': cloud.clock[0] - 10, **fields}
    return cloud.put(f'/posts/{post}', headers=headers, json=body)


def test_device_registration_needs_the_admin_key(cloud):
    assert cloud.post('/devices', json={'name': 'x'}).status_code == 401
    assert cloud.post('/devices', json={'name': 'x'}, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert 'token' in cloud.post('/devices', json={'name': 'x'}, headers={'Authorization': 'Bearer admin-secret'}).json()


def test_endpoints_require_a_known_device_token(cloud):
    for headers in ({}, {'Authorization': 'Bearer nope'}, {'Authorization': 'Basic abc'}):
        assert cloud.get('/snapshots', headers=headers).status_code == 401
        assert cloud.put('/accounts/c', headers=headers, json={}).status_code == 401
    assert cloud.get('/health').json() == {'ok': True}


def test_refresh_tokens_are_encrypted_at_rest_and_the_device_token_is_hashed(cloud):
    headers = enroll(cloud)
    assert account(cloud, headers).status_code == 200
    raw = cloud.db_path.read_bytes()
    for secret in (b'refresh-plain-123', b'csecret-plain', headers['Authorization'].split()[1].encode()):
        assert secret not in raw
    sealed = sqlite3.connect(cloud.db_path).execute('SELECT sealed FROM accounts').fetchone()[0]
    assert Vault(KEY).open(sealed)['refresh_token'] == 'refresh-plain-123'
    with pytest.raises(VaultError):
        Vault(Fernet.generate_key()).open(sealed)


def test_invalid_fernet_key_is_rejected_with_instructions(tmp_path):
    with pytest.raises(VaultError, match='Fernet'):
        create_app(tmp_path / 'x.sqlite3', 'not-a-key', 'a', poll_seconds=0)


def test_devices_cannot_see_each_others_data(cloud):
    one, two = enroll(cloud, 'one'), enroll(cloud, 'two')
    account(cloud, one)
    track(cloud, one)
    cloud.clock[0] += 3700
    assert cloud.app_ref.state.poller.run_once() == 1
    assert len(cloud.get('/snapshots', headers=one).json()['snapshots']) == 1
    assert cloud.get('/snapshots', headers=two).json()['snapshots'] == []
    assert track(cloud, two).status_code == 422  # channel not registered for device two


def test_poller_stores_metrics_and_retention_once_the_one_hour_mark_passes(cloud):
    headers = enroll(cloud)
    account(cloud, headers)
    track(cloud, headers)
    poller = cloud.app_ref.state.poller
    assert poller.run_once() == 0  # not due: first poll is at +1 h
    cloud.clock[0] += 3700
    assert poller.run_once() == 1
    snap = cloud.get('/snapshots', headers=headers).json()['snapshots'][0]
    assert snap['post_id'] == 'p1' and snap['metrics']['views'] == 7 and snap['metrics']['likes'] == 7
    assert snap['retention'] == ROWS and snap['fetched_at'] == cloud.clock[0]
    assert any(r.url.path == '/token' for r in cloud.google.requests)


def test_poll_schedule_decays_and_finishes(cloud):
    headers = enroll(cloud)
    account(cloud, headers)
    track(cloud, headers)
    store, poller = cloud.app_ref.state.store, cloud.app_ref.state.poller
    published = cloud.clock[0] - 10
    for expected in OFFSETS:
        row = store.get_post(store.device_for(headers['Authorization'].split()[1]), 'p1')
        assert row['next_poll_at'] == pytest.approx(published + expected)
        cloud.clock[0] = row['next_poll_at'] + 1
        assert poller.run_once() == 1
    row = store.get_post(store.device_for(headers['Authorization'].split()[1]), 'p1')
    assert row['next_poll_at'] is None and len(cloud.get('/snapshots', headers=headers).json()['snapshots']) == len(OFFSETS)


def test_scheduled_video_is_watched_until_it_goes_public(cloud):
    headers = enroll(cloud)
    account(cloud, headers)
    when = cloud.clock[0] + 7200
    cloud.google.privacy, cloud.google.publish_at = 'private', '2026-11-02T07:30:00Z'
    track(cloud, headers, published_at=None, scheduled_at=when)
    poller = cloud.app_ref.state.poller
    cloud.clock[0] = when + 130
    assert poller.run_once() == 1 and cloud.get('/snapshots', headers=headers).json()['snapshots'] == []
    cloud.google.privacy, cloud.google.publish_at = 'public', None
    cloud.clock[0] += 400
    poller.run_once()  # now public: waits for the 1 h mark
    assert cloud.get('/snapshots', headers=headers).json()['snapshots'] == []
    cloud.clock[0] += 3700
    poller.run_once()
    assert len(cloud.get('/snapshots', headers=headers).json()['snapshots']) == 1


def test_since_cursor_returns_only_newer_snapshots(cloud):
    headers = enroll(cloud)
    account(cloud, headers)
    track(cloud, headers)
    poller = cloud.app_ref.state.poller
    cloud.clock[0] += 3700
    poller.run_once()
    first = cloud.get('/snapshots', headers=headers).json()['snapshots']
    cursor = first[-1]['stored_at']
    assert cloud.get('/snapshots', params={'since': cursor}, headers=headers).json()['snapshots'] == []
    cloud.clock[0] += 6 * 3600
    poller.run_once()
    newer = cloud.get('/snapshots', params={'since': cursor}, headers=headers).json()['snapshots']
    assert len(newer) == 1 and newer[0]['stored_at'] > cursor


def test_google_rejection_backs_off_and_keeps_the_post(cloud):
    headers = enroll(cloud)
    account(cloud, headers)
    track(cloud, headers)
    poller, store = cloud.app_ref.state.poller, cloud.app_ref.state.store
    device = store.device_for(headers['Authorization'].split()[1])
    cloud.google.refresh_status = 400
    cloud.clock[0] += 3700
    poller.run_once()
    row = store.get_post(device, 'p1')
    assert row['attempts'] == 1 and 'Reconnect' in row['last_error'] and row['next_poll_at'] == pytest.approx(cloud.clock[0] + 900)
    cloud.clock[0] = row['next_poll_at'] + 1
    cloud.google.refresh_status = 200
    poller.run_once()
    assert store.get_post(device, 'p1')['attempts'] == 0 and len(cloud.get('/snapshots', headers=headers).json()['snapshots']) == 1


def test_removing_an_account_stops_tracking_its_posts(cloud):
    headers = enroll(cloud)
    account(cloud, headers)
    track(cloud, headers)
    assert cloud.delete('/accounts/ch1', headers=headers).status_code == 204
    cloud.clock[0] += 3700
    assert cloud.app_ref.state.poller.run_once() == 0


def test_https_can_be_required(tmp_path):
    app = create_app(tmp_path / 'h.sqlite3', KEY, 'admin', poll_seconds=0, require_https=True)
    with TestClient(app) as client:
        assert client.get('/snapshots').status_code == 400
        assert client.get('/health').status_code == 200
        assert client.get('/snapshots', headers={'x-forwarded-proto': 'https'}).status_code == 401
