"""YouTube adapter: PKCE, upload with a mid-upload 503, publishAt scheduling, metrics and retention parsing."""
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from publish_support import FakeGoogle, RETENTION_ROWS

from clippie.publish import youtube
from clippie.publish.base import AuthError, PublishError


def make(google, **kwargs):
    return youtube.YouTube(google.client(), 'cid', 'csecret', 'refresh-1', sleep=lambda s: None, **kwargs)


@pytest.fixture
def video(tmp_path):
    path = tmp_path / 'clip.mp4'
    path.write_bytes(bytes(range(256)) * 2400)  # 614,400 bytes: three 256 KiB chunks
    return path


def post(**fields):
    return {'id': 'p1', 'title': 'Why this works', 'description': 'A short story', 'hashtags': ['AI', '#learning'],
            'scheduled_at': None, 'created_at': time.time(), 'published_at': None, 'upload_session': None, **fields}


def test_pkce_challenge_is_s256_of_the_verifier():
    verifier, challenge = youtube.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    assert challenge == expected
    assert youtube.pkce_pair()[0] != verifier


def test_authorization_url_carries_pkce_scopes_and_loopback_redirect():
    url = youtube.authorization_url('cid', 'http://127.0.0.1:8000/api/publish/oauth/youtube/callback', 'st', 'chal')
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert query['code_challenge'] == 'chal' and query['code_challenge_method'] == 'S256'
    assert query['redirect_uri'].startswith('http://127.0.0.1:8000/') and query['access_type'] == 'offline'
    assert set(query['scope'].split()) == set(youtube.SCOPES) and len(youtube.SCOPES) == 3
    assert query['state'] == 'st' and query['response_type'] == 'code'


def test_code_exchange_sends_verifier_and_rejects_bad_codes():
    google = FakeGoogle()
    tokens = youtube.exchange_code(google.client(), 'cid', 'secret', 'code', 'verifier', 'http://127.0.0.1/cb')
    assert tokens['refresh_token'] == 'refresh-secret-xyz'
    sent = parse_qs(google.requests[0].content.decode())
    assert sent['code_verifier'] == ['verifier'] and sent['grant_type'] == ['authorization_code']
    google.token_error = 'Bad code'
    with pytest.raises(AuthError, match='Bad code'):
        youtube.exchange_code(google.client(), 'cid', 'secret', 'code', 'verifier', 'http://127.0.0.1/cb')


def test_resumable_upload_survives_a_mid_upload_503(video):
    google = FakeGoogle()
    google.fail = {1: 'before'}
    sleeps = []
    client = youtube.YouTube(google.client(), 'cid', 'csecret', 'refresh-1', sleep=sleeps.append, chunk_size=262144)
    progress = []
    handle = client.upload(post(), video, progress.append)
    assert handle == {'video_id': 'vid123'}
    assert bytes(google.received) == video.read_bytes()
    assert sleeps == [2] and google.sessions == 1
    assert progress[-1] == 1.0 and progress == sorted(progress)


def test_upload_recovers_when_the_server_stored_the_chunk_but_the_response_was_lost(video):
    google = FakeGoogle()
    google.fail = {0: 'after'}
    client = youtube.YouTube(google.client(), 'cid', 'csecret', 'refresh-1', sleep=lambda s: None, chunk_size=262144)
    client.upload(post(), video)
    assert bytes(google.received) == video.read_bytes()
    assert any(r.headers.get('content-range', '').startswith('bytes */') for r in google.requests)


def test_completed_saved_session_is_adopted_without_sending_again(video):
    import httpx
    google = FakeGoogle()
    original = google.handle
    def completed(request):
        if request.url.host == 'upload.test' and request.headers.get('content-range', '').startswith('bytes */'):
            google.requests.append(request)
            return httpx.Response(200, json={'id': 'already-published'})
        return original(request)
    client = youtube.YouTube(httpx.Client(transport=httpx.MockTransport(completed)), 'cid', 'secret', 'refresh', sleep=lambda _: None)
    assert client.upload(post(upload_session='https://upload.test/existing'), video) == {'video_id': 'already-published'}
    assert google.sessions == 0
    assert not [r for r in google.requests if r.url.host == 'upload.test' and not r.headers.get('content-range', '').startswith('bytes */')]


def test_upload_gives_up_after_repeated_5xx_with_a_retryable_error(video):
    google = FakeGoogle()
    google.fail = {i: 'before' for i in range(20)}
    client = youtube.YouTube(google.client(), 'cid', 'csecret', 'refresh-1', sleep=lambda s: None, chunk_size=262144, retries=2)
    with pytest.raises(PublishError) as error:
        client.upload(post(), video)
    assert error.value.retryable


def test_upload_resumes_a_saved_session_and_flags_an_expired_one(video):
    google = FakeGoogle()
    saved = []
    client = youtube.YouTube(google.client(), 'cid', 'csecret', 'refresh-1', sleep=lambda s: None, chunk_size=262144,
                             save_session=saved.append)
    client.upload(post(upload_session='https://upload.test/old'), video)
    assert saved == ['https://upload.test/old'] and google.sessions == 0
    google.expire_session = True
    with pytest.raises(PublishError) as error:
        client.upload(post(upload_session='https://upload.test/old'), video)
    assert not error.value.clear_session and not error.value.retryable and 'uncertain' in str(error.value)


def test_scheduled_post_is_private_with_publish_at_and_shorts_hashtag(video):
    google = FakeGoogle()
    when = time.time() + 86400
    make(google, chunk_size=262144).upload(post(scheduled_at=when), video)
    status = google.insert_body['status']
    assert status['privacyStatus'] == 'private'
    assert status['publishAt'] == youtube.rfc3339(when) and status['publishAt'].endswith('.000Z')
    snippet = google.insert_body['snippet']
    assert snippet['title'] == 'Why this works' and '#Shorts' in snippet['description']
    assert '#AI' in snippet['description'] and '#learning' in snippet['description'] and 'Shorts' in snippet['tags']


def test_immediate_post_is_public_without_publish_at(video):
    google = FakeGoogle()
    make(google).upload(post(scheduled_at=time.time() + 5), video)
    assert google.insert_body['status']['privacyStatus'] == 'public' and 'publishAt' not in google.insert_body['status']


def test_title_is_trimmed_and_shorts_tag_not_duplicated():
    meta = youtube.video_metadata(post(title='x' * 140, description='Nice #shorts', hashtags=['Shorts']))
    assert len(meta['snippet']['title']) == 100
    assert meta['snippet']['description'].lower().count('#shorts') == 1


def test_shorts_checks_flag_long_and_horizontal_clips():
    assert youtube.shorts_checks(45, 1080, 1920) == []
    assert 'Longer than 3 minutes' in youtube.shorts_checks(200, 1080, 1920)[0]
    assert 'vertical' in youtube.shorts_checks(45, 1920, 1080)[0]


def test_status_reports_scheduled_then_published():
    google = FakeGoogle()
    client = make(google)
    google.video = {'privacy': 'private', 'publishAt': '2026-11-02T07:30:00Z'}
    result = client.status({'external_id': 'vid123', 'scheduled_at': 1})
    assert result.status == 'scheduled' and result.published_at == pytest.approx(1793604600)
    google.video = {'privacy': 'public', 'publishAt': None}
    assert client.status({'external_id': 'vid123', 'scheduled_at': 1000.0}).status == 'published'


def test_metrics_combine_data_api_analytics_and_retention():
    google = FakeGoogle()
    result = make(google).metrics({'external_id': 'vid123', 'published_at': time.time() - 7200, 'created_at': time.time()})
    assert result.metrics['views'] == 130  # analytics overrides the Data API count
    assert result.metrics['avg_view_duration_s'] == 19 and result.metrics['avg_view_percentage'] == 63.5
    assert result.metrics['likes'] == 10 and result.metrics['shares'] == 4 and result.metrics['subscribers_gained'] == 3
    assert result.metrics['comments'] == 2 and result.metrics['watch_minutes'] == 41.5
    assert len(result.retention) == 11 and result.retention[0] == [0.0, 1.0, 0.5]
    report_calls = [r for r in google.requests if r.url.path == '/v2/reports']
    retention_call = [r for r in report_calls if r.url.params.get('dimensions')][0]
    assert retention_call.url.params['metrics'] == 'audienceWatchRatio,relativeRetentionPerformance'
    assert 'video==vid123' in retention_call.url.params['filters']


def test_retention_parser_sorts_and_keeps_missing_relative_values():
    body = {'columnHeaders': [{'name': 'elapsedVideoTimeRatio'}, {'name': 'audienceWatchRatio'}, {'name': 'relativeRetentionPerformance'}],
            'rows': [[0.5, 0.6, None], [0.0, 1.0, 0.4]]}
    assert youtube.parse_retention(body) == [[0.0, 1.0, 0.4], [0.5, 0.6, None]]
    assert youtube.parse_retention({}) == [] and youtube.parse_analytics({'columnHeaders': [], 'rows': []}) == {}


def test_expired_token_is_refreshed_once_on_401():
    google = FakeGoogle()
    calls = {'n': 0}
    inner = google.handle

    def flaky(request):
        if request.url.path == '/youtube/v3/channels':
            calls['n'] += 1
            if calls['n'] == 1:
                import httpx
                return httpx.Response(401, json={'error': {'message': 'expired'}})
        return inner(request)
    import httpx
    client = youtube.YouTube(httpx.Client(transport=httpx.MockTransport(flaky)), 'cid', 'csecret', 'refresh-1')
    assert client.connect()[0].external_id == 'UC123' and google.refreshes == 2


def test_quota_errors_are_retryable_and_auth_errors_are_not():
    import httpx
    quota = httpx.Response(403, json={'error': {'message': 'quota', 'errors': [{'reason': 'quotaExceeded'}]}})
    with pytest.raises(PublishError) as error:
        youtube.YouTube.checked(quota)
    assert error.value.retryable
    with pytest.raises(AuthError):
        youtube.YouTube.checked(httpx.Response(403, json={'error': {'message': 'forbidden'}}))


def test_retention_falls_back_to_all_viewers_then_says_not_available():
    google = FakeGoogle()
    original = google.report

    def report(request, empty=('ORGANIC',)):
        if request.url.params.get('dimensions') and any(e in request.url.params['filters'] for e in empty):
            return httpx.Response(200, json={'columnHeaders': [], 'rows': []})
        return original(request)
    google.report = report
    posted = {'external_id': 'vid123', 'published_at': time.time() - 7200, 'created_at': time.time()}
    result = make(google).metrics(posted)
    assert len(result.retention) == 11 and result.metrics['retention_audience'] == 'all'
    google.report = lambda request: report(request, empty=('vid123',))
    result = make(google).metrics(posted)
    assert result.retention is None and result.metrics['retention_audience'] == 'not_available_yet'
