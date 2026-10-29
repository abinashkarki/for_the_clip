"""Publishing API: settings and secrets, OAuth, posts, scheduling end to end, metrics, retention, performance, cloud."""
from datetime import datetime, timedelta, timezone
import time

import pytest

from publish_support import (connect_aggregator, connect_youtube, configure_youtube, fake_engine, finished_post, publish,  # noqa: F401
                             studio, video_bytes,
                             wait_for_render)

from clippie.publish.secrets import SERVICE

API = '/api/publish'


def future(hours=24):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_settings_never_echo_secrets_and_store_them_in_the_keychain(publish, isolated_credential_store):
    client = publish.studio.client
    reply = client.put(f'{API}/settings', json={'youtube_client_id': 'cid', 'youtube_client_secret': 'super-secret-value',
                                                'aggregator_key': 'agg-key', 'cloud_url': 'https://cloud.example.com/',
                                                'cloud_device_token': 'device-token'})
    assert reply.status_code == 200
    body = reply.json()
    assert body['youtube_client_id'] == 'cid' and body['youtube_client_secret_set'] and body['aggregator_key_set']
    assert body['cloud_url'] == 'https://cloud.example.com' and body['cloud_device_token_set']
    assert 'super-secret-value' not in reply.text and 'device-token' not in client.get(f'{API}/settings').text
    assert isolated_credential_store.values[SERVICE, 'youtube_client_secret'] == 'super-secret-value'
    assert b'super-secret-value' not in publish.db.path.read_bytes()
    client.put(f'{API}/settings', json={'aggregator_key': ''})
    assert client.get(f'{API}/settings').json()['aggregator_key_set'] is False


def test_settings_reject_plain_http_cloud_urls_and_unknown_fields(publish):
    client = publish.studio.client
    assert client.put(f'{API}/settings', json={'cloud_url': 'http://example.com'}).status_code == 422
    assert client.put(f'{API}/settings', json={'surprise': 1}).status_code == 422


def test_connect_needs_client_credentials_first(publish):
    assert publish.studio.client.post(f'{API}/channels/youtube/connect').status_code == 422


def test_youtube_oauth_flow_stores_tokens_only_in_the_keychain(publish, isolated_credential_store):
    configure_youtube(publish)
    client = publish.studio.client
    started = client.post(f'{API}/channels/youtube/connect').json()
    assert started['redirect_uri'].endswith('/api/publish/oauth/youtube/callback')
    assert 'code_challenge_method=S256' in started['authorization_url'] and 'cid.apps' in started['authorization_url']
    reply = client.get(f'{API}/oauth/youtube/callback', params={'code': 'auth-code', 'state': started['state']})
    assert reply.status_code == 200 and 'Connected' in reply.text
    channel = client.get(f'{API}/channels').json()['channels'][0]
    assert channel['display_name'] == 'My Channel' and channel['external_id'] == 'UC123' and len(channel['scopes']) == 3
    ref = publish.db.get_channel(channel['id'])['token_ref']
    assert isolated_credential_store.values[SERVICE, ref] == 'refresh-secret-xyz'
    for blob in (publish.db.path.read_bytes(), client.get(f'{API}/channels').content):
        assert b'refresh-secret-xyz' not in blob and b'access-1' not in blob
    exchange = [r for r in publish.google.requests if r.url.path == '/token'][0]
    assert b'code_verifier=' in exchange.content and b'redirect_uri=' in exchange.content


def test_oauth_callback_rejects_unknown_replayed_and_denied_states(publish):
    configure_youtube(publish)
    client = publish.studio.client
    assert client.get(f'{API}/oauth/youtube/callback', params={'code': 'x', 'state': 'nope'}).status_code == 400
    started = client.post(f'{API}/channels/youtube/connect').json()
    denied = client.get(f'{API}/oauth/youtube/callback', params={'error': 'access_denied', 'state': started['state']})
    assert denied.status_code == 400 and 'access_denied' in denied.text
    assert client.get(f'{API}/oauth/youtube/callback', params={'code': 'x', 'state': started['state']}).status_code == 400
    assert client.get(f'{API}/channels').json()['channels'] == []


def test_oauth_failure_from_google_is_shown_and_nothing_is_saved(publish):
    configure_youtube(publish)
    client = publish.studio.client
    publish.google.token_error = 'Malformed auth code.'
    state = client.post(f'{API}/channels/youtube/connect').json()['state']
    reply = client.get(f'{API}/oauth/youtube/callback', params={'code': 'x', 'state': state})
    assert reply.status_code == 400 and 'Malformed auth code' in reply.text
    assert client.get(f'{API}/channels').json()['channels'] == []


def test_disconnect_revokes_and_removes_the_token(publish, isolated_credential_store):
    channel = connect_youtube(publish)
    assert publish.studio.client.delete(f'{API}/channels/{channel["id"]}').status_code == 204
    assert publish.google.revoked == ['refresh-secret-xyz']
    assert not [k for k in isolated_credential_store.values if k[1].startswith('youtube:')]
    assert publish.studio.client.get(f'{API}/channels').json()['channels'] == []
    assert publish.studio.client.delete(f'{API}/channels/{channel["id"]}').status_code == 404


def test_aggregator_connect_creates_tiktok_and_instagram_accounts(publish, isolated_credential_store):
    channels = connect_aggregator(publish)
    assert {c['platform'] for c in channels} == {'tiktok', 'instagram'} and all(c['provider'] == 'aggregator' for c in channels)
    assert isolated_credential_store.values[SERVICE, 'aggregator_key'] == 'agg-key'
    bad = publish.studio.client.post(f'{API}/channels/aggregator/connect', json={'api_key': 'wrong'})
    assert bad.status_code == 422


def test_create_post_triggers_a_1080p_export_of_that_revision(publish):
    connect_youtube(publish)
    post, short, revision = finished_post(publish, when=future())
    assert post['revision_id'] == revision['id'] and post['status'] == 'scheduled' and post['artifact_id']
    render = publish.studio.engine.renders[0]
    assert render['preview'] is False
    view = publish.studio.client.get(f'{API}/posts/{post["id"]}').json()
    assert view['rendering'] is False and view['hashtags'] == ['ai'] and view['scheduled_at'].endswith('Z')


def test_create_post_reuses_an_existing_1080p_export(publish):
    connect_youtube(publish)
    _, short, revision = publish.studio.clip()
    job = publish.studio.client.post(f'/api/studio/clips/{short["id"]}/export',
                                     json={'revision_id': revision['id'], 'quality': '1080p'}).json()['job_id']
    publish.studio.wait(job)
    reply = publish.studio.client.post(f'{API}/posts', json={
        'short_id': short['id'], 'revision_id': revision['id'], 'platforms': ['youtube'], 'title': 'T'})
    post = reply.json()['posts'][0]
    assert post['artifact_id'] and post['rendering'] is False and len(publish.studio.engine.renders) == 1


def test_preview_exports_are_not_reused_for_publishing(publish):
    connect_youtube(publish)
    _, short, revision = publish.studio.clip()
    job = publish.studio.client.post(f'/api/studio/clips/{short["id"]}/export',
                                     json={'revision_id': revision['id'], 'quality': 'preview'}).json()['job_id']
    publish.studio.wait(job)
    reply = publish.studio.client.post(f'{API}/posts', json={
        'short_id': short['id'], 'revision_id': revision['id'], 'platforms': ['youtube'], 'title': 'T'})
    assert reply.json()['posts'][0]['rendering'] is True


def test_create_post_validation(publish):
    connect_youtube(publish)
    client = publish.studio.client
    _, short, revision = publish.studio.clip()
    base = {'short_id': short['id'], 'revision_id': revision['id'], 'platforms': ['youtube'], 'title': 'T'}
    assert client.post(f'{API}/posts', json={**base, 'revision_id': 'missing'}).status_code == 404
    assert client.post(f'{API}/posts', json={**base, 'short_id': 'missing'}).status_code == 404
    assert client.post(f'{API}/posts', json={**base, 'platforms': ['tiktok']}).status_code == 422
    assert client.post(f'{API}/posts', json={**base, 'scheduled_at': '2020-01-01T00:00:00Z'}).status_code == 422
    assert client.post(f'{API}/posts', json={**base, 'scheduled_at': '2030-01-01T10:00:00'}).status_code == 422
    assert client.post(f'{API}/posts', json={**base, 'scheduled_at': 'soon'}).status_code == 422
    assert client.post(f'{API}/posts', json={**base, 'title': '   '}).status_code == 422
    assert client.post(f'{API}/posts', json={**base, 'extra': 1}).status_code == 422
    assert client.get(f'{API}/posts').json()['posts'] == []


def test_timezone_offsets_are_normalised_to_utc(publish):
    connect_youtube(publish)
    when = (datetime.now(timezone.utc) + timedelta(days=2)).astimezone(timezone(timedelta(hours=11)))
    post, _, _ = finished_post(publish, when=when.isoformat(timespec='seconds'))
    assert post['scheduled_at'] == pytest.approx(when.timestamp(), abs=1)


def test_mislabeled_export_dimensions_are_not_reused_for_short_checks(publish):
    connect_youtube(publish)
    from clippie import studio_api
    _, short, revision = publish.studio.clip()
    job = publish.studio.client.post(f'/api/studio/clips/{short["id"]}/export',
                                     json={'revision_id': revision['id'], 'quality': '1080p'}).json()['job_id']
    publish.studio.wait(job)
    export = studio_api.studio().latest_export(short['id'], revision['id'])
    studio_api.studio()._put('exports', {**export, 'duration': 240.0, 'width': 1920, 'height': 1080})
    reply = publish.studio.client.post(f'{API}/posts', json={'short_id': short['id'], 'revision_id': revision['id'],
                                                              'platforms': ['youtube'], 'title': 'T', 'draft': True})
    warnings = reply.json()['posts'][0]['warnings']
    assert warnings == []
    assert reply.json()['posts'][0]['rendering'] is False  # Saving a draft defers final rendering.
    # Metadata claimed a different duration/aspect; a new verified render is required.
    assert reply.json()['posts'][0]['artifact_id'] is None


def test_draft_is_not_uploaded_until_published(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish, draft=True)
    assert post['status'] == 'draft'
    assert publish.studio.engine.renders == []
    publish.service.run_once()
    assert publish.db.get_post(post['id'])['external_id'] is None
    reply = publish.studio.client.post(f'{API}/posts/{post["id"]}/publish', json={})
    assert reply.json()['status'] == 'scheduled'
    wait_for_render(publish, post['id'])
    publish.service.run_once()
    assert publish.db.get_post(post['id'])['external_id'] == 'vid123'
    assert publish.studio.client.post(f'{API}/posts/{post["id"]}/publish', json={}).status_code == 409


def test_scheduled_publish_end_to_end_with_metrics_and_performance(publish):
    connect_youtube(publish)
    when = datetime.now(timezone.utc) + timedelta(hours=5)
    post, short, revision = finished_post(publish, when=when.strftime('%Y-%m-%dT%H:%M:%SZ'))
    publish.service.run_once()
    uploaded = publish.db.get_post(post['id'])
    assert uploaded['status'] == 'scheduled' and uploaded['external_id'] == 'vid123' and uploaded['upload_session'] is None
    status = publish.google.insert_body['status']
    assert status['privacyStatus'] == 'private' and status['publishAt'].startswith(when.strftime('%Y-%m-%dT%H:%M'))
    assert bytes(publish.google.received)[4:8] == b'ftyp'
    # YouTube publishes by itself while the laptop sleeps; the next check notices
    publish.google.video = {'privacy': 'public', 'publishAt': status['publishAt']}
    clock = when.timestamp() + 600
    publish.service.now = lambda: clock
    publish.service.run_once()
    published = publish.db.get_post(post['id'])
    assert published['status'] == 'published' and published['published_at'] == pytest.approx(when.timestamp(), abs=1)
    assert published['url'].endswith('/vid123')
    clock = published['published_at'] + 3700
    publish.service.run_once()
    reply = publish.studio.client.get(f'{API}/posts/{post["id"]}/metrics').json()
    assert reply['latest']['metrics']['views'] == 130 and len(reply['history']) == 1
    performance = publish.studio.client.get(f'{API}/clips/{short["id"]}/performance').json()
    assert performance['totals']['views'] == 130 and performance['totals']['shares'] == 4
    assert performance['revisions'] == [{'revision_id': revision['id'], 'posts': 1, 'views': 130}]
    assert performance['posts'][0]['metrics']['avg_view_percentage'] == 63.5
    assert performance['current_revision_id'] == revision['id']


def test_retention_maps_onto_the_published_revisions_words(publish):
    connect_youtube(publish)
    post, short, revision = finished_post(publish)
    publish.db.add_retention(post['id'], [[0, 1.0, 0.5], [0.55, 0.9, 0.5], [0.65, 0.5, 0.2], [1.0, 0.4, 0.3]])
    body = publish.studio.client.get(f'{API}/posts/{post["id"]}/retention').json()
    assert body['duration'] == 8.0 and body['revision_id'] == revision['id']
    words = body['words']
    assert [w['text'] for w in words][:3] == ['So', 'how', 'um'] and words[0]['watch_ratio'] > 0.99
    dropped = [words[i]['text'] for i in body['drops']]
    assert 'this' in dropped and 'So' not in dropped
    assert publish.studio.client.get(f'{API}/posts/missing/retention').status_code == 404


def test_retention_without_data_is_empty_not_an_error(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    body = publish.studio.client.get(f'{API}/posts/{post["id"]}/retention').json()
    assert body['points'] == [] and body['words'] == [] and body['fetched_at'] is None and body['audience'] is None
    publish.db.add_snapshot(post['id'], {'views': 3, 'retention_audience': 'not_available_yet'})
    body = publish.studio.client.get(f'{API}/posts/{post["id"]}/retention').json()
    assert body['audience'] == 'not_available_yet'


def test_tiktok_and_instagram_posts_go_through_the_aggregator(publish):
    connect_aggregator(publish)
    studio = publish.studio
    _, short, revision = studio.clip()
    reply = studio.client.post(f'{API}/posts', json={'short_id': short['id'], 'revision_id': revision['id'],
                                                      'platforms': ['instagram', 'tiktok'], 'title': 'Reel'})
    posts = reply.json()['posts']
    assert [p['platform'] for p in posts] == ['instagram', 'tiktok']
    for post in posts:
        wait_for_render(publish, post['id'])
    publish.service.run_once()
    assert [p['platforms'][0] for p in publish.aggregator.posted] == ['instagram', 'tiktok']
    states = {p['platform']: p for p in studio.client.get(f'{API}/posts').json()['posts']}
    assert states['instagram']['status'] == 'published' and states['instagram']['url'] == 'https://instagram.test/p/1'
    assert len(publish.studio.engine.renders) == 1  # one render shared by both platforms


def test_post_edit_delete_and_retry_rules(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish, when=future(), draft=True)
    client = publish.studio.client
    patched = client.patch(f'{API}/posts/{post["id"]}', json={'title': 'New', 'hashtags': ['a', '#b'], 'clear_schedule': True}).json()
    assert patched['title'] == 'New' and patched['hashtags'] == ['a', 'b'] and patched['scheduled_at'] is None
    assert client.post(f'{API}/posts/{post["id"]}/retry').status_code == 409
    client.post(f'{API}/posts/{post["id"]}/publish', json={})
    wait_for_render(publish, post['id'])
    publish.service.run_once()
    assert client.patch(f'{API}/posts/{post["id"]}', json={'title': 'Late'}).status_code == 409
    assert client.delete(f'{API}/posts/{post["id"]}').status_code == 409  # uploaded, waiting to go live
    publish.db.update_post(post['id'], status='published')
    assert client.delete(f'{API}/posts/{post["id"]}').status_code == 204
    assert client.get(f'{API}/posts/{post["id"]}').status_code == 404


def test_failed_post_can_be_retried(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    publish.db.update_post(post['id'], status='failed', error='boom', attempts=6)
    reply = publish.studio.client.post(f'{API}/posts/{post["id"]}/retry').json()
    assert reply['status'] == 'scheduled' and reply['error'] is None
    publish.service.run_once()
    assert publish.db.get_post(post['id'])['external_id'] == 'vid123'


def test_manual_metric_sync_polls_one_post_now(publish):
    connect_youtube(publish)
    publish.google.video = {'privacy': 'public', 'publishAt': None}
    post, _, _ = finished_post(publish)
    assert publish.studio.client.post(f'{API}/posts/{post["id"]}/sync').status_code == 409
    publish.service.run_once()
    published = publish.db.get_post(post['id'])
    assert published['status'] == 'published'
    reply = publish.studio.client.post(f'{API}/posts/{post["id"]}/sync')
    assert reply.status_code == 200 and reply.json()['metrics']['views'] == 130


def test_channel_with_waiting_posts_cannot_be_disconnected(publish):
    channel = connect_youtube(publish)
    finished_post(publish, when=future())
    assert publish.studio.client.delete(f'{API}/channels/{channel["id"]}').status_code == 409


def test_cloud_sync_registers_accounts_and_merges_snapshots_once(publish):
    channel = connect_youtube(publish)
    client = publish.studio.client
    assert client.post(f'{API}/cloud/sync').status_code == 422
    client.put(f'{API}/settings', json={'cloud_url': 'https://cloud.example.com', 'cloud_device_token': 'device-token'})
    post, _, _ = finished_post(publish)
    publish.service.run_once()
    publish.cloud.snapshots = [{'post_id': post['id'], 'fetched_at': 1000.0, 'stored_at': 1001.0,
                                'metrics': {'views': 77}, 'retention': [[0.0, 1.0, None], [1.0, 0.5, None]]},
                               {'post_id': 'gone', 'fetched_at': 1.0, 'stored_at': 1002.0, 'metrics': {}}]
    first = client.post(f'{API}/cloud/sync').json()
    assert first == {'posts_registered': 1, 'snapshots_merged': 1}
    assert publish.cloud.accounts[channel['id']]['refresh_token'] == 'refresh-secret-xyz'
    assert publish.cloud.posts[post['id']]['external_id'] == 'vid123'
    assert client.post(f'{API}/cloud/sync').json()['snapshots_merged'] == 0
    latest = client.get(f'{API}/posts/{post["id"]}/metrics').json()['latest']
    assert latest['source'] == 'cloud' and latest['metrics']['views'] == 77
    assert client.get(f'{API}/posts/{post["id"]}/retention').json()['points'][1] == [1.0, 0.5, None]
    assert publish.cloud.requests[-1].url.params['since'] == '1002.0'


def test_cloud_sync_reports_a_bad_device_token(publish):
    connect_youtube(publish)
    publish.studio.client.put(f'{API}/settings', json={'cloud_url': 'https://cloud.example.com', 'cloud_device_token': 'wrong'})
    reply = publish.studio.client.post(f'{API}/cloud/sync')
    assert reply.status_code == 502 and 'device token' in reply.json()['detail']


def test_status_counts_posts(publish):
    connect_youtube(publish)
    finished_post(publish, draft=True)
    body = publish.studio.client.get(f'{API}/status').json()
    assert body == {'posts': {'draft': 1}, 'channels': 1, 'cloud_configured': False}
