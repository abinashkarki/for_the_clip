"""Minimal YouTube metrics client for the control plane: token refresh, statistics, analytics, retention."""
from datetime import datetime, timezone

import httpx

TOKEN_URL = 'https://oauth2.googleapis.com/token'
VIDEOS = 'https://www.googleapis.com/youtube/v3/videos'
REPORTS = 'https://youtubeanalytics.googleapis.com/v2/reports'
ANALYTICS = {'views': 'views', 'estimatedMinutesWatched': 'watch_minutes', 'averageViewDuration': 'avg_view_duration_s',
             'averageViewPercentage': 'avg_view_percentage', 'likes': 'likes', 'shares': 'shares',
             'subscribersGained': 'subscribers_gained', 'comments': 'comments'}
STATISTICS = {'viewCount': 'views', 'likeCount': 'likes', 'commentCount': 'comments'}


class UpstreamError(RuntimeError):
    def __init__(self, message, auth=False):
        super().__init__(message)
        self.auth = auth


def refresh(http, creds):
    try:
        response = http.post(TOKEN_URL, data={'grant_type': 'refresh_token', 'refresh_token': creds['refresh_token'],
                                               'client_id': creds['client_id'], 'client_secret': creds['client_secret']})
    except httpx.HTTPError as error:
        raise UpstreamError(f'Could not reach Google: {error.__class__.__name__}') from None
    if response.status_code != 200:
        raise UpstreamError('Google refused the refresh token. Reconnect the channel in Clippie.', auth=response.status_code < 500)
    return response.json()['access_token']


def get(http, token, url, params):
    try:
        response = http.get(url, params=params, headers={'Authorization': f'Bearer {token}'})
    except httpx.HTTPError as error:
        raise UpstreamError(f'Could not reach YouTube: {error.__class__.__name__}') from None
    if response.status_code >= 300:
        raise UpstreamError(f'YouTube returned HTTP {response.status_code}', auth=response.status_code in (401, 403))
    return response.json()


def rows(body):
    names = [c['name'] for c in body.get('columnHeaders') or []]
    return [dict(zip(names, row)) for row in body.get('rows') or []]


def video_state(http, token, video_id):
    """(privacyStatus, publishAt epoch|None, statistics) or None when the video is gone."""
    body = get(http, token, VIDEOS, {'part': 'statistics,status', 'id': video_id})
    if not body.get('items'):
        return None
    item = body['items'][0]
    stats = {STATISTICS[k]: int(v) for k, v in (item.get('statistics') or {}).items() if k in STATISTICS}
    when = (item.get('status') or {}).get('publishAt')
    stamp = datetime.fromisoformat(when.replace('Z', '+00:00')).timestamp() if when else None
    return (item.get('status') or {}).get('privacyStatus'), stamp, stats


def metrics(http, token, video_id, since, now, stats):
    day = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d')
    common = {'ids': 'channel==MINE', 'startDate': day(since), 'endDate': day(now)}
    out = dict(stats)
    report = rows(get(http, token, REPORTS, {**common, 'filters': f'video=={video_id}', 'metrics': ','.join(ANALYTICS)}))
    if report:
        out.update({ANALYTICS[k]: v for k, v in report[0].items() if k in ANALYTICS})
    curve = rows(get(http, token, REPORTS, {**common, 'dimensions': 'elapsedVideoTimeRatio',
                                            'filters': f'video=={video_id};audienceType==ORGANIC',
                                            'metrics': 'audienceWatchRatio,relativeRetentionPerformance'}))
    points = sorted([float(r['elapsedVideoTimeRatio']), float(r.get('audienceWatchRatio') or 0.0),
                     None if r.get('relativeRetentionPerformance') is None else float(r['relativeRetentionPerformance'])]
                    for r in curve if r.get('elapsedVideoTimeRatio') is not None)
    return out, points or None
