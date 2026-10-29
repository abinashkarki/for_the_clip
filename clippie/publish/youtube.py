"""YouTube adapter: installed-app OAuth (PKCE, loopback), resumable upload, scheduling, Data and Analytics metrics.

Scheduling uses `status.privacyStatus=private` plus `status.publishAt`, so YouTube itself publishes the video
while the laptop is off. Shorts are vertical videos of at most 3 minutes; `#Shorts` is added to the description.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from .base import AuthError, ChannelInfo, MetricsResult, PublishError, PublishResult, Publisher

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
API = 'https://www.googleapis.com/youtube/v3'
UPLOAD_URL = 'https://www.googleapis.com/upload/youtube/v3/videos'
ANALYTICS = 'https://youtubeanalytics.googleapis.com/v2/reports'
SCOPES = ['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly',
          'https://www.googleapis.com/auth/yt-analytics.readonly']
CHUNK = 8 * 1024 * 1024  # multiple of 256 KiB, as the resumable protocol requires
MAX_SHORT_SECONDS = 180
TITLE_LIMIT, DESCRIPTION_LIMIT = 100, 5000


# -- OAuth ------------------------------------------------------------------

def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def pkce_pair():
    verifier = b64url(secrets.token_bytes(48))
    return verifier, b64url(hashlib.sha256(verifier.encode()).digest())


def authorization_url(client_id, redirect_uri, state, challenge):
    query = {'client_id': client_id, 'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': ' '.join(SCOPES),
             'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256', 'access_type': 'offline',
             'prompt': 'consent', 'include_granted_scopes': 'true'}
    return f'{AUTH_URL}?{urlencode(query)}'


def token_request(http, form):
    try:
        response = http.post(TOKEN_URL, data=form)
    except httpx.HTTPError as error:
        raise PublishError(f'Could not reach Google: {error.__class__.__name__}.', retryable=True) from None
    if response.status_code >= 500:
        raise PublishError('Google sign-in is temporarily unavailable.', retryable=True)
    body = response.json() if response.content else {}
    if response.status_code != 200:
        reason = body.get('error_description') or body.get('error') or f'HTTP {response.status_code}'
        raise AuthError(f'Google rejected the sign-in: {reason}')
    return body


def exchange_code(http, client_id, client_secret, code, verifier, redirect_uri):
    return token_request(http, {'grant_type': 'authorization_code', 'code': code, 'code_verifier': verifier,
                                'redirect_uri': redirect_uri, 'client_id': client_id, 'client_secret': client_secret})


def refresh_access_token(http, client_id, client_secret, refresh_token):
    return token_request(http, {'grant_type': 'refresh_token', 'refresh_token': refresh_token,
                                'client_id': client_id, 'client_secret': client_secret})


# -- Shorts conventions -----------------------------------------------------

def shorts_checks(duration, width=None, height=None):
    """Warnings for a clip that YouTube would not treat as a Short."""
    problems = []
    if duration is not None and duration > MAX_SHORT_SECONDS:
        problems.append(f'Longer than {MAX_SHORT_SECONDS // 60} minutes: YouTube will not list it as a Short.')
    if width and height and height < width:
        problems.append('Not vertical or square: YouTube will not list it as a Short.')
    return problems


def clean_tag(value):
    return re.sub(r'[^\w]', '', value.lstrip('#'), flags=re.UNICODE)


def shorts_text(post):
    title = ' '.join(post['title'].split())[:TITLE_LIMIT]
    tags = [t for t in (clean_tag(x) for x in post.get('hashtags') or []) if t]
    if not any(t.lower() == 'shorts' for t in tags):
        tags.append('Shorts')
    body = post.get('description') or ''
    tail = ' '.join('#' + t for t in tags if f'#{t.lower()}' not in body.lower())
    description = (body.rstrip() + ('\n\n' if body.strip() and tail else '') + tail)[:DESCRIPTION_LIMIT]
    return title, description, tags


def rfc3339(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def video_metadata(post, now=None):
    title, description, tags = shorts_text(post)
    status = {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False}
    when = post.get('scheduled_at')
    if when and when > (now or time.time()) + 60:
        status.update(privacyStatus='private', publishAt=rfc3339(when))
    return {'snippet': {'title': title, 'description': description, 'tags': tags, 'categoryId': '22'}, 'status': status}


# -- parsing ----------------------------------------------------------------

ANALYTICS_MAP = {'views': 'views', 'estimatedMinutesWatched': 'watch_minutes', 'averageViewDuration': 'avg_view_duration_s',
                 'averageViewPercentage': 'avg_view_percentage', 'likes': 'likes', 'shares': 'shares',
                 'subscribersGained': 'subscribers_gained', 'comments': 'comments'}
STATISTICS_MAP = {'viewCount': 'views', 'likeCount': 'likes', 'commentCount': 'comments'}


def parse_statistics(body):
    """Data API videos.list -> (canonical metrics, privacyStatus, publishAt epoch or None)."""
    items = body.get('items') or []
    if not items:
        return None
    item = items[0]
    metrics = {}
    for key, name in STATISTICS_MAP.items():
        if key in (item.get('statistics') or {}):
            metrics[name] = int(item['statistics'][key])
    status = item.get('status') or {}
    return metrics, status.get('privacyStatus'), parse_time(status.get('publishAt'))


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def parse_report(body):
    """Analytics report -> list of row dicts keyed by column name."""
    names = [c['name'] for c in body.get('columnHeaders') or []]
    return [dict(zip(names, row)) for row in body.get('rows') or []]


def parse_analytics(body):
    rows = parse_report(body)
    if not rows:
        return {}
    return {ANALYTICS_MAP[k]: v for k, v in rows[0].items() if k in ANALYTICS_MAP}


def parse_retention(body):
    """Retention report -> sorted [[elapsedVideoTimeRatio, audienceWatchRatio, relativeRetentionPerformance]]."""
    points = []
    for row in parse_report(body):
        ratio = row.get('elapsedVideoTimeRatio')
        if ratio is None:
            continue
        points.append([float(ratio), float(row.get('audienceWatchRatio') or 0.0),
                       float(row['relativeRetentionPerformance']) if row.get('relativeRetentionPerformance') is not None else None])
    return sorted(points)


# -- client -----------------------------------------------------------------

class YouTube(Publisher):
    name = 'youtube'

    def __init__(self, http, client_id, client_secret, refresh_token=None, save_session=None, sleep=time.sleep,
                 chunk_size=CHUNK, retries=5, now=time.time):
        self.http, self.client_id, self.client_secret = http, client_id, client_secret
        self.refresh_token, self.save_session = refresh_token, save_session
        self.sleep, self.chunk_size, self.retries, self.now = sleep, chunk_size, retries, now
        self._access, self._expires = None, 0.0

    # -- auth
    def access_token(self, force=False):
        if self._access and not force and self.now() < self._expires - 60:
            return self._access
        if not self.refresh_token:
            raise AuthError('This YouTube channel needs to be connected again.')
        body = refresh_access_token(self.http, self.client_id, self.client_secret, self.refresh_token)
        self._access = body['access_token']
        self._expires = self.now() + float(body.get('expires_in', 3600))
        return self._access

    def call(self, method, url, **kwargs):
        """Authorised API call: one token refresh on 401, readable errors, 5xx and network failures retryable."""
        extra = kwargs.pop('headers', {})
        for attempt in (0, 1):
            headers = {'Authorization': f'Bearer {self.access_token(force=attempt == 1)}', **extra}
            try:
                response = self.http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as error:
                raise PublishError(f'Could not reach YouTube: {error.__class__.__name__}.', retryable=True) from None
            if response.status_code == 401 and attempt == 0:
                continue
            return self.checked(response)

    @staticmethod
    def checked(response):
        if response.status_code < 300:
            return response
        try:
            error = response.json().get('error', {})
            text = error.get('message') or f'HTTP {response.status_code}'
            reasons = {e.get('reason') for e in error.get('errors') or []}
        except ValueError:
            text, reasons = f'HTTP {response.status_code}', set()
        if response.status_code in (401, 403) and not reasons & {'quotaExceeded', 'rateLimitExceeded', 'uploadLimitExceeded'}:
            raise AuthError(f'YouTube refused the request: {text}')
        retryable = response.status_code >= 500 or response.status_code == 429 or bool(reasons & {'quotaExceeded', 'rateLimitExceeded'})
        raise PublishError(f'YouTube error: {text}', retryable=retryable)

    # -- Publisher
    def connect(self, **_):
        body = self.call('GET', f'{API}/channels', params={'part': 'snippet', 'mine': 'true'}).json()
        return [ChannelInfo('youtube', item['snippet']['title'], item['id'], SCOPES) for item in body.get('items') or []]

    def upload(self, post, media_path, progress=None):
        path = Path(media_path)
        size = path.stat().st_size
        existing = post.get('upload_session')
        session = existing or self.start_session(post, size)
        if self.save_session:
            self.save_session(session)
        offset = 0
        if existing:
            offset, completed = self.query_session(session, size)
            if completed is not None:
                return {'video_id': completed['id']}
        body = self.send(session, path, size, progress, offset=offset)
        return {'video_id': body['id']}

    def start_session(self, post, size):
        response = self.call('POST', UPLOAD_URL, params={'uploadType': 'resumable', 'part': 'snippet,status'},
                             headers={'X-Upload-Content-Length': str(size), 'X-Upload-Content-Type': 'video/mp4',
                                      'Content-Type': 'application/json; charset=UTF-8'},
                             content=json.dumps(video_metadata(post, self.now())))
        location = response.headers.get('location')
        if not location:
            raise PublishError('YouTube did not start an upload session.', retryable=True)
        return location

    def send(self, session, path, size, progress, offset=0):
        """Resumable PUT loop. A 5xx or dropped connection asks YouTube how much it has, then continues."""
        failures = 0
        with path.open('rb') as stream:
            while True:
                stream.seek(offset)
                chunk = stream.read(self.chunk_size)
                end = offset + len(chunk) - 1
                headers = {'Content-Length': str(len(chunk)), 'Content-Type': 'video/mp4',
                           'Content-Range': f'bytes {offset}-{end}/{size}' if chunk else f'bytes */{size}'}
                try:
                    response = self.http.put(session, headers=headers, content=chunk)
                    broken = response.status_code >= 500
                except httpx.HTTPError:
                    response, broken = None, True
                if broken:
                    failures += 1
                    if failures > self.retries:
                        raise PublishError('YouTube kept failing during the upload. It will be retried later.', retryable=True)
                    self.sleep(min(2 ** failures, 60))
                    offset, completed = self.query_session(session, size)
                    if completed is not None:
                        return completed
                    continue
                failures = 0
                if response.status_code in (200, 201):
                    progress and progress(1.0)
                    return response.json()
                if response.status_code == 308:
                    offset = self.range_end(response)
                    progress and progress(min(offset / size, 0.99))
                    continue
                if response.status_code == 404:
                    raise PublishError('Upload result is uncertain: the YouTube session expired. Check YouTube before trying again.')
                self.checked(response)
                raise PublishError(f'Unexpected upload response {response.status_code}.', retryable=True)

    def query_session(self, session, size):
        try:
            response = self.http.put(session, headers={'Content-Length': '0', 'Content-Range': f'bytes */{size}'})
        except httpx.HTTPError:
            raise PublishError('Could not check the existing YouTube upload. Retrying the check later.', retryable=True) from None
        if response.status_code == 308:
            offset = self.range_end(response)
            if offset >= size:
                raise PublishError('YouTube received the video but has not confirmed completion. Retrying the check later.', retryable=True)
            return offset, None
        if response.status_code in (200, 201):
            body = response.json()
            if body.get('id'):
                return size, body
            raise PublishError('Upload result is uncertain. Check YouTube before trying again.')
        if response.status_code in (404, 410):
            raise PublishError('Upload result is uncertain: the YouTube session expired. Check YouTube before trying again.')
        if response.status_code >= 500 or response.status_code == 429:
            raise PublishError('Could not check the existing YouTube upload. Retrying the check later.', retryable=True)
        self.checked(response)
        raise PublishError('Upload result is uncertain. Check YouTube before trying again.')

    @staticmethod
    def range_end(response):
        match = re.fullmatch(r'bytes=0-(\d+)', response.headers.get('range', ''))
        return int(match.group(1)) + 1 if match else 0

    def schedule(self, post, handle):
        """The upload already carried `publishAt` or `public`; read back what YouTube recorded."""
        found = self.status({**post, 'external_id': handle['video_id']})
        if found is None:
            raise PublishError('YouTube has no record of the uploaded video yet.', retryable=True)
        return found

    def status(self, post):
        body = self.call('GET', f'{API}/videos', params={'part': 'statistics,status', 'id': post['external_id']}).json()
        parsed = parse_statistics(body)
        if parsed is None:
            return None
        _, privacy, publish_at = parsed
        url = f'https://youtube.com/shorts/{post["external_id"]}'
        if privacy == 'public':
            return PublishResult('published', post['external_id'], url, publish_at or post.get('scheduled_at') or self.now())
        return PublishResult('scheduled', post['external_id'], url, publish_at)

    def metrics(self, post):
        video = post['external_id']
        body = self.call('GET', f'{API}/videos', params={'part': 'statistics,status', 'id': video}).json()
        parsed = parse_statistics(body)
        if parsed is None:
            raise PublishError('YouTube has no record of this video.')
        metrics = dict(parsed[0])
        start = datetime.fromtimestamp(post.get('published_at') or post['created_at'], timezone.utc).strftime('%Y-%m-%d')
        end = datetime.fromtimestamp(self.now(), timezone.utc).strftime('%Y-%m-%d')
        common = {'ids': 'channel==MINE', 'startDate': start, 'endDate': end}
        report = self.call('GET', ANALYTICS, params={**common, 'filters': f'video=={video}', 'metrics': ','.join(ANALYTICS_MAP)})
        metrics.update(parse_analytics(report.json()))
        # Organic viewers are the honest signal; a new or small Short often has no organic breakdown yet, so the
        # curve for all viewers is used then. Both are empty until YouTube Analytics catches up (usually 2-3 days).
        points = None
        for audience, filters in (('organic', f'video=={video};audienceType==ORGANIC'), ('all', f'video=={video}')):
            retention = self.call('GET', ANALYTICS, params={**common, 'dimensions': 'elapsedVideoTimeRatio',
                                                            'filters': filters,
                                                            'metrics': 'audienceWatchRatio,relativeRetentionPerformance'})
            points = parse_retention(retention.json())
            if points:
                metrics['retention_audience'] = audience
                break
        else:
            metrics['retention_audience'] = 'not_available_yet'
        return MetricsResult(metrics, points or None)

    def revoke(self):
        if self.refresh_token:
            try:
                self.http.post(REVOKE_URL, data={'token': self.refresh_token})
            except httpx.HTTPError:
                pass
