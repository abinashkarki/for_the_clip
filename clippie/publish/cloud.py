"""Client for the always-on cloud control plane (`cloud/`): register channels and posts, pull metric snapshots."""
import httpx

from .base import PublishError
from . import runtime


class CloudClient:
    def __init__(self, http, url, device_token):
        self.http, self.url = http, url.rstrip('/')
        self.headers = {'Authorization': f'Bearer {device_token}'}

    def call(self, method, path, **kwargs):
        try:
            response = self.http.request(method, self.url + path, headers=self.headers, **kwargs)
        except httpx.HTTPError as error:
            raise PublishError(f'Could not reach the cloud service: {error.__class__.__name__}.', retryable=True) from None
        if response.status_code in (401, 403):
            raise PublishError('The cloud service rejected this device token.')
        if response.status_code >= 400:
            raise PublishError(f'The cloud service returned HTTP {response.status_code}.', retryable=response.status_code >= 500)
        return response.json() if response.content else {}


def configured(db):
    return bool(db.setting('cloud_url') and runtime.SECRETS.get('cloud_device_token'))


def sync(db, http):
    """Push YouTube channels and tracked posts, then pull new snapshots. Returns counts."""
    client = CloudClient(http, db.setting('cloud_url'), runtime.SECRETS.get('cloud_device_token'))
    client_id, client_secret = db.setting('youtube_client_id'), runtime.SECRETS.get('youtube_client_secret')
    pushed = 0
    for channel in db.channels():
        refresh = runtime.SECRETS.get(channel['token_ref'] or '')
        if channel['provider'] != 'youtube' or not refresh or not client_id or not client_secret:
            continue
        client.call('PUT', f'/accounts/{channel["id"]}', json={'platform': 'youtube', 'refresh_token': refresh,
                                                               'client_id': client_id, 'client_secret': client_secret})
    for post in db.posts():
        if post['platform'] != 'youtube' or not post['external_id'] or post['status'] not in {'scheduled', 'published'}:
            continue
        client.call('PUT', f'/posts/{post["id"]}', json={
            'channel_id': post['channel_id'], 'platform': 'youtube', 'external_id': post['external_id'],
            'published_at': post['published_at'], 'scheduled_at': post['scheduled_at'], 'created_at': post['created_at']})
        pushed += 1
    since = db.setting('cloud_since', 0.0)
    body = client.call('GET', '/snapshots', params={'since': since})
    merged, latest = 0, since
    for item in body.get('snapshots') or []:
        latest = max(latest, item['stored_at'])
        try:
            db.get_post(item['post_id'])
        except KeyError:
            continue
        if db.has_snapshot(item['post_id'], item['fetched_at'], 'cloud'):
            continue
        db.add_snapshot(item['post_id'], item['metrics'], item['fetched_at'], source='cloud')
        if item.get('retention'):
            db.add_retention(item['post_id'], item['retention'], item['fetched_at'])
        merged += 1
    db.set_setting('cloud_since', latest)
    return {'posts_registered': pushed, 'snapshots_merged': merged}
