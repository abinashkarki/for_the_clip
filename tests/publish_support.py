"""Fake Google, aggregator and cloud servers behind httpx.MockTransport for the publishing tests. No network."""
import json
import re
from urllib.parse import parse_qs

import httpx

RETENTION_ROWS = [[i / 10, round(1 - i * 0.07, 3), 0.5 + i / 40] for i in range(11)]


class FakeGoogle:
    """OAuth token endpoint, YouTube Data/Analytics APIs and a resumable upload endpoint."""

    def __init__(self):
        self.requests = []
        self.received = bytearray()
        self.sessions = 0
        self.fail = {}          # put index (0-based) -> 'before' | 'after'
        self.puts = 0
        self.video = {'privacy': 'private', 'publishAt': None}  # tests set 'public' to mimic an immediate publish
        self.token_error = None
        self.channel = {'id': 'UC123', 'snippet': {'title': 'My Channel'}}
        self.insert_body = None
        self.expire_session = False
        self.refreshes = 0
        self.stats = {'viewCount': '120', 'likeCount': '9', 'commentCount': '2'}
        self.analytics_error = None
        self.revoked = []

    def transport(self):
        return httpx.MockTransport(self.handle)

    def client(self):
        return httpx.Client(transport=self.transport())

    def handle(self, request):
        self.requests.append(request)
        url = str(request.url)
        path = request.url.path
        if url.startswith('https://oauth2.googleapis.com/token'):
            return self.token(request)
        if url.startswith('https://oauth2.googleapis.com/revoke'):
            self.revoked.append(parse_qs(request.content.decode())['token'][0])
            return httpx.Response(200)
        if path == '/upload/youtube/v3/videos':
            self.insert_body = json.loads(request.content)
            self.sessions += 1
            return httpx.Response(200, headers={'location': f'https://upload.test/session{self.sessions}'})
        if request.url.host == 'upload.test':
            return self.put(request)
        if path == '/youtube/v3/channels':
            return httpx.Response(200, json={'items': [self.channel]})
        if path == '/youtube/v3/videos':
            return httpx.Response(200, json={'items': [{'id': 'vid123', 'statistics': self.stats, 'status': {
                'privacyStatus': self.video['privacy'], **({'publishAt': self.video['publishAt']} if self.video['publishAt'] else {})}}]})
        if path == '/v2/reports':
            return self.report(request)
        return httpx.Response(404, json={'error': {'message': 'unexpected ' + url}})

    def token(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if self.token_error:
            return httpx.Response(400, json={'error': 'invalid_grant', 'error_description': self.token_error})
        if form['grant_type'] == 'authorization_code':
            return httpx.Response(200, json={'access_token': 'access-1', 'refresh_token': 'refresh-secret-xyz', 'expires_in': 3600})
        self.refreshes += 1
        return httpx.Response(200, json={'access_token': f'access-r{self.refreshes}', 'expires_in': 3600})

    def put(self, request):
        header = request.headers['content-range']
        if self.expire_session:
            return httpx.Response(404)
        if header.startswith('bytes */'):
            if not self.received:
                return httpx.Response(308)
            return httpx.Response(308, headers={'range': f'bytes=0-{len(self.received) - 1}'})
        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', header)
        start, end, total = map(int, match.groups())
        index, self.puts = self.puts, self.puts + 1
        mode = self.fail.get(index)
        if mode == 'before':
            return httpx.Response(503)
        if start == len(self.received):
            self.received.extend(request.content)
        if mode == 'after':
            return httpx.Response(503)
        if len(self.received) >= total:
            return httpx.Response(200, json={'id': 'vid123'})
        return httpx.Response(308, headers={'range': f'bytes=0-{len(self.received) - 1}'})

    def report(self, request):
        query = request.url.params
        if self.analytics_error:
            return httpx.Response(self.analytics_error, json={'error': {'message': 'analytics down'}})
        if query.get('dimensions') == 'elapsedVideoTimeRatio':
            return httpx.Response(200, json={'columnHeaders': [{'name': 'elapsedVideoTimeRatio'}, {'name': 'audienceWatchRatio'},
                                                               {'name': 'relativeRetentionPerformance'}], 'rows': RETENTION_ROWS})
        names = query['metrics'].split(',')
        values = {'views': 130, 'estimatedMinutesWatched': 41.5, 'averageViewDuration': 19, 'averageViewPercentage': 63.5,
                  'likes': 10, 'shares': 4, 'subscribersGained': 3, 'comments': 2}
        return httpx.Response(200, json={'columnHeaders': [{'name': n} for n in names], 'rows': [[values[n] for n in names]]})


class FakeAggregator:
    """Ayrshare-compatible endpoints."""

    def __init__(self):
        self.requests = []
        self.posted = []
        self.media = bytearray()
        self.status_value = 'success'
        self.analytics = {'instagram': {'status': 'success', 'analytics': {
            'views': 900, 'reach': 700, 'saved': 12, 'sharesCount': 8, 'ig_reels_avg_watch_time': 4500, 'reels_skip_rate': 0.31}}}
        self.fail_post = None

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host == 'media.test':
            self.media.extend(request.content)
            return httpx.Response(200)
        if request.headers.get('authorization') != 'Bearer agg-key':
            return httpx.Response(401, json={'status': 'error', 'message': 'bad key'})
        if path == '/api/user':
            return httpx.Response(200, json={'activeSocialAccounts': ['instagram', 'tiktok', 'facebook'], 'displayNames': [
                {'platform': 'instagram', 'displayName': 'My IG', 'userId': 'ig1'},
                {'platform': 'tiktok', 'displayName': 'My TT', 'userId': 'tt1'}]})
        if path == '/api/media/uploadUrl':
            return httpx.Response(200, json={'uploadUrl': 'https://media.test/put', 'accessUrl': 'https://media.test/get/v.mp4'})
        if path == '/api/post' and request.method == 'POST':
            if self.fail_post:
                return httpx.Response(self.fail_post, json={'status': 'error', 'message': 'nope'})
            payload = json.loads(request.content)
            self.posted.append(payload)
            platform = payload['platforms'][0]
            return httpx.Response(200, json={'status': 'success', 'id': 'agg-1',
                                             'postIds': [{'platform': platform, 'id': 'native-1', 'postUrl': f'https://{platform}.test/p/1'}]})
        if path == '/api/post/agg-1':
            return httpx.Response(200, json={'status': self.status_value, 'postIds': [{'platform': 'instagram', 'postUrl': 'https://instagram.test/p/1'}]})
        if path == '/api/analytics/post':
            return httpx.Response(200, json=self.analytics)
        return httpx.Response(404, json={'status': 'error', 'message': 'unexpected ' + path})


# -- fixtures ---------------------------------------------------------------
import pytest  # noqa: E402

from test_studio_support import fake_engine, studio, video_bytes  # noqa: E402,F401


class Fakes:
    def __init__(self):
        self.google, self.aggregator, self.cloud = FakeGoogle(), FakeAggregator(), FakeCloud()

    def handle(self, request):
        host = request.url.host
        if host in {'app.ayrshare.com', 'media.test'}:
            return self.aggregator.handle(request)
        if host == 'cloud.example.com':
            return self.cloud.handle(request)
        return self.google.handle(request)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))


class FakeCloud:
    def __init__(self):
        self.requests, self.accounts, self.posts, self.snapshots = [], {}, {}, []

    def handle(self, request):
        self.requests.append(request)
        if request.headers.get('authorization') != 'Bearer device-token':
            return httpx.Response(401, json={'detail': 'bad token'})
        parts = request.url.path.strip('/').split('/')
        if request.method == 'PUT' and parts[0] == 'accounts':
            self.accounts[parts[1]] = json.loads(request.content)
            return httpx.Response(200, json={'ok': True})
        if request.method == 'PUT' and parts[0] == 'posts':
            self.posts[parts[1]] = json.loads(request.content)
            return httpx.Response(200, json={'ok': True})
        if parts[0] == 'snapshots':
            since = float(request.url.params.get('since', 0))
            return httpx.Response(200, json={'snapshots': [s for s in self.snapshots if s['stored_at'] > since]})
        return httpx.Response(404, json={'detail': 'unexpected'})


@pytest.fixture
def publish(studio, monkeypatch):
    """The studio app with fake Google/aggregator/cloud servers and a manually driven scheduler."""
    from clippie import studio_api
    from clippie.publish import runtime, sync
    fakes = Fakes()
    monkeypatch.setattr(runtime, 'http_factory', fakes.client)
    monkeypatch.setattr(runtime, 'sleep', lambda seconds: None)
    monkeypatch.setattr(sync, 'ensure_started', lambda root: None)
    monkeypatch.setattr(sync, 'kick', lambda root: None)
    runtime.reset()
    fakes.studio = studio
    fakes.root = studio_api.data_root()
    fakes.service = sync.service(fakes.root)
    fakes.db = runtime.store(fakes.root)
    yield fakes
    sync.stop_all()
    runtime.reset()


def configure_youtube(fakes):
    client = fakes.studio.client
    assert client.put('/api/publish/settings', json={'youtube_client_id': 'cid.apps.googleusercontent.com',
                                                      'youtube_client_secret': 'csecret'}).status_code == 200


def connect_youtube(fakes):
    configure_youtube(fakes)
    client = fakes.studio.client
    started = client.post('/api/publish/channels/youtube/connect').json()
    reply = client.get('/api/publish/oauth/youtube/callback', params={'code': 'auth-code', 'state': started['state']})
    assert reply.status_code == 200, reply.text
    return client.get('/api/publish/channels').json()['channels'][0]


def connect_aggregator(fakes):
    reply = fakes.studio.client.post('/api/publish/channels/aggregator/connect', json={'api_key': 'agg-key'})
    assert reply.status_code == 200, reply.text
    return reply.json()['channels']


def finished_post(fakes, platform='youtube', when=None, **extra):
    """Create a post for the seeded clip and wait for its export; returns (post, short, revision)."""
    studio = fakes.studio
    _, short, revision = studio.clip()
    body = {'short_id': short['id'], 'revision_id': revision['id'], 'platforms': [platform], 'title': 'Big idea',
            'description': 'Why it matters', 'hashtags': ['ai'], **extra}
    if when:
        body['scheduled_at'] = when
    reply = studio.client.post('/api/publish/posts', json=body)
    assert reply.status_code == 201, reply.text
    post = reply.json()['posts'][0]
    wait_for_render(fakes, post['id'])
    return fakes.db.get_post(post['id']), short, revision


def wait_for_render(fakes, post_id):
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job_id = fakes.db.get_post(post_id)['render_job_id']
        if job_id is None:
            return
        fakes.studio.wait(job_id)
        fakes.service.attach_renders()
    raise AssertionError('render did not attach')
