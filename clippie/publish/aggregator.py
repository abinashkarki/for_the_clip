"""Aggregator adapter for TikTok and Instagram Reels, speaking the Ayrshare REST API.

Used until direct TikTok (content-posting audit) and Instagram (Meta app review) adapters exist; they implement the
same `Publisher` interface. The base URL is configurable so any Ayrshare-compatible service works.
"""
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .base import AuthError, ChannelInfo, MetricsResult, PublishError, PublishResult, Publisher

BASE_URL = 'https://app.ayrshare.com/api'
PLATFORMS = ('tiktok', 'instagram')
# Aggregator field names -> canonical metric names. Instagram Reels exposes views, reach,
# ig_reels_avg_watch_time (ms), reels_skip_rate, saves and shares; TikTok exposes views, likes, comments, shares.
ALIASES = {
    'views': 'views', 'plays': 'views', 'videoViews': 'views', 'viewCount': 'views', 'ig_reels_video_view_total_time': None,
    'reach': 'reach', 'likes': 'likes', 'likeCount': 'likes', 'comments': 'comments', 'commentCount': 'comments',
    'commentsCount': 'comments', 'shares': 'shares', 'shareCount': 'shares', 'sharesCount': 'shares',
    'saves': 'saves', 'saved': 'saves', 'savedCount': 'saves', 'reels_skip_rate': 'skip_rate', 'skipRate': 'skip_rate',
    'ig_reels_avg_watch_time': 'avg_view_duration_s', 'avgWatchTime': 'avg_view_duration_s',
    'averageTimeWatched': 'avg_view_duration_s', 'followersGained': 'subscribers_gained',
}
MS_FIELDS = {'ig_reels_avg_watch_time'}


def normalise_metrics(raw):
    """Flatten an aggregator analytics object into canonical metrics (durations in seconds)."""
    metrics = {}
    for key, value in raw.items():
        name = ALIASES.get(key)
        if name and isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[name] = value / 1000.0 if key in MS_FIELDS else value
    return metrics


def iso_utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Aggregator(Publisher):
    name = 'aggregator'

    def __init__(self, http, api_key, base_url=BASE_URL):
        self.http, self.api_key, self.base_url = http, api_key, base_url.rstrip('/')

    def call(self, method, path, **kwargs):
        try:
            response = self.http.request(method, self.base_url + path, headers={'Authorization': f'Bearer {self.api_key}'},
                                         **kwargs)
        except httpx.HTTPError as error:
            raise PublishError(f'Could not reach the posting service: {error.__class__.__name__}.', retryable=True) from None
        if response.status_code in (401, 403):
            raise AuthError('The posting service rejected the API key. Check it on the Channels page.')
        body = response.json() if response.content else {}
        if response.status_code >= 500 or response.status_code == 429:
            raise PublishError('The posting service is busy. Retrying later.', retryable=True)
        if response.status_code >= 400 or (isinstance(body, dict) and body.get('status') == 'error'):
            raise PublishError(self.error_text(body, response.status_code))
        return body

    @staticmethod
    def error_text(body, status):
        if isinstance(body, dict):
            if body.get('message'):
                return str(body['message'])
            errors = body.get('errors') or body.get('posts') or []
            if errors and isinstance(errors[0], dict):
                return str(errors[0].get('message') or errors[0])
        return f'The posting service returned HTTP {status}.'

    def connect(self, **_):
        body = self.call('GET', '/user')
        names = {d.get('platform'): d for d in body.get('displayNames') or []}
        active = body.get('activeSocialAccounts') or list(names)
        found = []
        for platform in PLATFORMS:
            if platform in active:
                info = names.get(platform, {})
                found.append(ChannelInfo(platform, info.get('displayName') or info.get('username') or platform.title(),
                                         str(info.get('userId') or info.get('id') or platform), []))
        return found

    def upload(self, post, media_path, progress=None):
        path = Path(media_path)
        meta = self.call('GET', '/media/uploadUrl', params={'fileName': f'{post["id"]}.mp4', 'contentType': 'video/mp4'})
        try:
            with path.open('rb') as stream:
                response = self.http.put(meta['uploadUrl'], content=stream.read(), headers={'Content-Type': 'video/mp4'})
        except httpx.HTTPError as error:
            raise PublishError(f'Media upload failed: {error.__class__.__name__}.', retryable=True) from None
        if response.status_code >= 300:
            raise PublishError(f'Media upload failed with HTTP {response.status_code}.', retryable=response.status_code >= 500)
        progress and progress(1.0)
        return {'media_url': meta['accessUrl']}

    def schedule(self, post, handle):
        title = ' '.join(post['title'].split())
        tags = ' '.join('#' + t.lstrip('#') for t in post.get('hashtags') or [])
        text = '\n\n'.join(part for part in (title, post.get('description') or '', tags) if part)
        payload = {'post': text, 'platforms': [post['platform']], 'mediaUrls': [handle['media_url']], 'isVideo': True}
        if post['platform'] == 'instagram':
            payload['instagramOptions'] = {'reels': True, 'shareReelsFeed': True}
        if post['platform'] == 'tiktok':
            payload['tikTokOptions'] = {'title': title[:90]}
        when = post.get('scheduled_at')
        future = bool(when and when > datetime.now(timezone.utc).timestamp() + 60)
        if future:
            payload['scheduleDate'] = iso_utc(when)
        try:
            body = self.call('POST', '/post', json=payload)
        except PublishError as error:
            if error.retryable:
                raise PublishError('Upload result is uncertain. Check the platform before trying again.') from None
            raise
        entry = next((p for p in body.get('postIds') or [] if p.get('platform') == post['platform']), {})
        external = body.get('id') or entry.get('id')
        if not external:
            raise PublishError('Upload result is uncertain. Check the platform before trying again.')
        if future:
            return PublishResult('scheduled', external, entry.get('postUrl'), when)
        return PublishResult('published', external, entry.get('postUrl'), datetime.now(timezone.utc).timestamp())

    def status(self, post):
        body = self.call('GET', f'/post/{post["external_id"]}')
        if not body:
            return None
        state = str(body.get('status') or '').lower()
        url = next((p.get('postUrl') for p in body.get('postIds') or [] if p.get('platform') == post['platform']), post.get('url'))
        if state in {'success', 'published', 'posted'}:
            return PublishResult('published', post['external_id'], url, post.get('scheduled_at') or datetime.now(timezone.utc).timestamp())
        if state in {'error', 'failed'}:
            raise PublishError(self.error_text(body, 200))
        return PublishResult('scheduled', post['external_id'], url, post.get('scheduled_at'))

    def metrics(self, post):
        body = self.call('POST', '/analytics/post', json={'id': post['external_id'], 'platforms': [post['platform']]})
        section = body.get(post['platform']) or {}
        if section.get('status') == 'error':
            raise PublishError(str(section.get('message') or 'Analytics are not available yet.'), retryable=True)
        raw = section.get('analytics') or section
        return MetricsResult(normalise_metrics(raw) if isinstance(raw, dict) else {}, None)
