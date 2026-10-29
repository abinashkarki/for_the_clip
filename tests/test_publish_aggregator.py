"""Aggregator (Ayrshare-compatible) client: connect, media upload, schedule, status, analytics."""
import time

import pytest

from publish_support import FakeAggregator

from clippie.publish.aggregator import Aggregator, normalise_metrics
from clippie.publish.base import AuthError, PublishError, Publisher


def make(fake, key='agg-key'):
    return Aggregator(fake.client(), key)


def post(platform='instagram', **fields):
    return {'id': 'p1', 'platform': platform, 'title': 'Hook line', 'description': 'More', 'hashtags': ['ai'],
            'scheduled_at': None, 'external_id': 'agg-1', 'url': None, **fields}


def test_aggregator_implements_the_publisher_interface():
    assert issubclass(Aggregator, Publisher)
    assert {'connect', 'upload', 'schedule', 'status', 'metrics'} <= set(Aggregator.__abstractmethods__ | set(dir(Aggregator)))


def test_connect_lists_only_tiktok_and_instagram_profiles():
    channels = make(FakeAggregator()).connect()
    assert [(c.platform, c.display_name, c.external_id) for c in channels] == [('tiktok', 'My TT', 'tt1'), ('instagram', 'My IG', 'ig1')]


def test_bad_key_is_an_auth_error():
    with pytest.raises(AuthError):
        make(FakeAggregator(), key='wrong').connect()


def test_upload_puts_media_then_schedule_posts_a_reel(tmp_path):
    fake, video = FakeAggregator(), tmp_path / 'v.mp4'
    video.write_bytes(b'abc' * 100)
    client = make(fake)
    handle = client.upload(post(), video)
    assert bytes(fake.media) == video.read_bytes() and handle == {'media_url': 'https://media.test/get/v.mp4'}
    result = client.schedule(post(), handle)
    payload = fake.posted[0]
    assert payload['platforms'] == ['instagram'] and payload['mediaUrls'] == [handle['media_url']] and payload['isVideo']
    assert payload['instagramOptions']['reels'] is True and 'scheduleDate' not in payload
    assert '#ai' in payload['post'] and result.status == 'published' and result.external_id == 'agg-1'


def test_scheduled_post_sends_schedule_date_in_utc():
    fake = FakeAggregator()
    when = time.time() + 7200
    result = make(fake).schedule(post('tiktok', scheduled_at=when), {'media_url': 'https://media.test/get/v.mp4'})
    assert fake.posted[0]['scheduleDate'].endswith('Z') and 'tikTokOptions' in fake.posted[0]
    assert result.status == 'scheduled' and result.published_at == when


def test_post_failure_after_dispatch_is_uncertain_and_not_retried():
    fake = FakeAggregator()
    fake.fail_post = 503
    with pytest.raises(PublishError) as error:
        make(fake).schedule(post(), {'media_url': 'u'})
    assert not error.value.retryable and 'uncertain' in str(error.value)
    fake.fail_post = 400
    with pytest.raises(PublishError, match='nope') as error:
        make(fake).schedule(post(), {'media_url': 'u'})
    assert not error.value.retryable


def test_status_maps_service_states():
    fake = FakeAggregator()
    assert make(fake).status(post()).status == 'published'
    fake.status_value = 'pending'
    assert make(fake).status(post()).status == 'scheduled'
    fake.status_value = 'error'
    with pytest.raises(PublishError):
        make(fake).status(post())


def test_instagram_reel_metrics_are_normalised():
    result = make(FakeAggregator()).metrics(post())
    assert result.metrics == {'views': 900, 'reach': 700, 'saves': 12, 'shares': 8, 'avg_view_duration_s': 4.5, 'skip_rate': 0.31}
    assert result.retention is None
    assert normalise_metrics({'likeCount': 3, 'junk': 'x', 'plays': True}) == {'likes': 3}


def test_analytics_error_is_retryable():
    fake = FakeAggregator()
    fake.analytics = {'instagram': {'status': 'error', 'message': 'not ready'}}
    with pytest.raises(PublishError) as error:
        make(fake).metrics(post())
    assert error.value.retryable
