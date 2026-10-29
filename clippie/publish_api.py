"""Publishing API (docs/v3/PUBLISHING.md) mounted at /api/publish.

Posts are tied to the exact clip revision and export artifact they publish. Tokens and keys live in the Keychain
(`publish/secrets.py`); this module returns only whether they are set. Uploads, scheduling and metric polling run on
the background service in `publish/sync.py`.
"""
from datetime import datetime, timezone
import html
import json
import secrets as token_tools
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import Field, StrictBool, StrictStr, field_validator

from . import studio_api
from .publish import cloud, runtime, sync, youtube
from .publish.aggregator import PLATFORMS as AGGREGATED
from .publish.base import AuthError, PublishError
from .publish.retention import map_words
from .publish.secrets import SecretError
from .studio_api import Strict, clip_context, collections, data_root, not_found

router = APIRouter(prefix='/api/publish', tags=['publish'])
PLATFORM_NAMES = ('youtube', *AGGREGATED)
SHORT_TITLE = 100
RENDER_QUALITY = '1080p'
CALLBACK_PATH = '/api/publish/oauth/youtube/callback'


# -- plumbing ---------------------------------------------------------------

def db():
    return runtime.store(data_root())


def iso(stamp):
    return None if stamp is None else datetime.fromtimestamp(float(stamp), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def upstream(error):
    """Map adapter failures to HTTP: configuration/auth 422, platform trouble 502."""
    return HTTPException(422 if isinstance(error, AuthError) else 502, str(error))


def channel_view(channel):
    return {'id': channel['id'], 'platform': channel['platform'], 'provider': channel['provider'],
            'display_name': channel['display_name'], 'external_id': channel['external_id'], 'scopes': channel['scopes'],
            'created_at': iso(channel['created_at'])}


def project_ref(post, cache=None):
    """Resolve (project_id, project_name, project_deleted) live, persisting the snapshot when it changes.

    Live when the short's collection exists and has no `trashed_at`; otherwise the stored snapshot is used
    (falling back to the collection itself when trashed, and to None when nothing is known)."""
    cache = {} if cache is None else cache
    if post['short_id'] not in cache:
        collection = None
        try:
            collection = collections().get_collection(collections().get_short(post['short_id'])['collection_id'])
        except KeyError:
            pass
        cache[post['short_id']] = collection
    collection = cache[post['short_id']]
    stored = (post.get('project_id'), post.get('project_name'))
    if collection is None:
        return stored[0], stored[1], True
    live = (collection['id'], studio_api.project_name(collection))
    trashed = bool(collection.get('trashed_at'))
    if stored[0] is None or (not trashed and stored != live):
        try:
            db().set_project(post['id'], *live)
        except Exception:
            pass
    if trashed:
        return (stored[0] or live[0]), (stored[1] or live[1]), True
    return live[0], live[1], False


def post_view(post, with_metrics=True, cache=None):
    names = {'id', 'platform', 'channel_id', 'short_id', 'revision_id', 'artifact_id', 'title', 'description', 'hashtags',
             'status', 'external_id', 'url', 'error', 'warnings', 'duration'}
    view = {name: post[name] for name in names}
    view.update(scheduled_at=iso(post['scheduled_at']), published_at=iso(post['published_at']),
                created_at=iso(post['created_at']), rendering=bool(post['render_job_id']) and post['status'] in ('scheduled', 'draft'))
    view['project_id'], view['project_name'], view['project_deleted'] = project_ref(post, cache)
    view['export_recovered'] = False
    view['export_preview_url'] = None
    if post['status'] == 'failed' and post.get('render_job_id') and not post.get('external_id'):
        try:
            job = studio_api.studio().get_job(post['render_job_id'])
            view['export_recovered'] = (job.get('status') == 'done' and job.get('clip_id') == post['short_id']
                                        and (job.get('payload') or {}).get('revision_id') == post['revision_id']
                                        and str((job.get('result') or {}).get('video_url') or '').startswith('/api/studio/files/'))
            if view['export_recovered']:
                view['export_preview_url'] = job['result']['video_url']
        except KeyError:
            pass
    try:
        view['channel_name'] = db().get_channel(post['channel_id'])['display_name']
    except KeyError:
        view['channel_name'] = None
    if with_metrics:
        latest = db().latest_snapshot(post['id'])
        view['metrics'] = latest['metrics'] if latest else None
        view['metrics_at'] = iso(latest['fetched_at']) if latest else None
    return view


def get_post(post_id):
    try:
        return db().get_post(post_id)
    except KeyError:
        raise not_found('Post not found.') from None


def parse_when(value):
    if value is None:
        return None
    try:
        when = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise HTTPException(422, 'scheduled_at must be an ISO 8601 time with a timezone, such as 2026-11-02T18:30:00+11:00.') from None
    if when.tzinfo is None:
        raise HTTPException(422, 'scheduled_at needs a timezone offset, such as +11:00 or Z.')
    stamp = when.timestamp()
    if stamp < time.time() - 300:
        raise HTTPException(422, 'scheduled_at is in the past. Pick a future time or publish now.')
    return stamp if stamp > time.time() + 60 else None


# -- settings ---------------------------------------------------------------

class SettingsBody(Strict):
    youtube_client_id: StrictStr | None = Field(default=None, max_length=300)
    youtube_client_secret: StrictStr | None = Field(default=None, max_length=300)
    aggregator_key: StrictStr | None = Field(default=None, max_length=300)
    aggregator_base_url: StrictStr | None = Field(default=None, max_length=300)
    cloud_url: StrictStr | None = Field(default=None, max_length=300)
    cloud_device_token: StrictStr | None = Field(default=None, max_length=300)


def settings_view():
    store = db()
    return {'youtube_client_id': store.setting('youtube_client_id') or '',
            'youtube_client_secret_set': bool(runtime.SECRETS.get('youtube_client_secret')),
            'aggregator_key_set': bool(runtime.SECRETS.get('aggregator_key')),
            'aggregator_base_url': store.setting('aggregator_base_url') or '',
            'cloud_url': store.setting('cloud_url') or '',
            'cloud_device_token_set': bool(runtime.SECRETS.get('cloud_device_token')),
            'redirect_uri_hint': CALLBACK_PATH}


def clean_url(value):
    value = value.strip().rstrip('/')
    if value and not value.startswith(('https://', 'http://127.0.0.1', 'http://localhost')):
        raise HTTPException(422, 'Use an https:// address.')
    return value


@router.get('/settings')
def get_settings():
    return settings_view()


@router.put('/settings')
def put_settings(body: SettingsBody):
    store = db()
    try:
        for field in ('youtube_client_secret', 'aggregator_key', 'cloud_device_token'):
            value = getattr(body, field)
            if value is not None:
                runtime.SECRETS.delete(field) if not value.strip() else runtime.SECRETS.set(field, value)
    except (SecretError, ValueError) as error:
        raise HTTPException(503 if isinstance(error, SecretError) else 422, str(error)) from None
    for field in ('youtube_client_id', 'aggregator_base_url', 'cloud_url'):
        value = getattr(body, field)
        if value is not None:
            store.set_setting(field, (clean_url(value) if field != 'youtube_client_id' else value.strip()) or None)
    return settings_view()


# -- channels ---------------------------------------------------------------

@router.get('/channels')
def list_channels():
    return {'channels': [channel_view(c) for c in db().channels()], 'settings': settings_view()}


def callback_uri(request):
    return str(request.base_url).rstrip('/') + CALLBACK_PATH


@router.post('/channels/youtube/connect')
def connect_youtube(request: Request):
    client_id = db().setting('youtube_client_id')
    if not client_id or not runtime.SECRETS.get('youtube_client_secret'):
        raise HTTPException(422, 'Add your Google Cloud OAuth client ID and secret first.')
    verifier, challenge = youtube.pkce_pair()
    state = token_tools.token_urlsafe(24)
    redirect = callback_uri(request)
    db().put_state(state, 'youtube', verifier, redirect)
    return {'authorization_url': youtube.authorization_url(client_id, redirect, state, challenge), 'state': state,
            'redirect_uri': redirect}


def finish_page(ok, message):
    colour = '#4fd6a0' if ok else '#ff7d72'
    target = '/studio/#/channels'
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><title>Clippie</title>'
        '<body style="background:#0b0d11;color:#eceef3;font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0">'
        f'<div style="max-width:420px;text-align:center"><h2 style="color:{colour}">{"Connected" if ok else "Not connected"}</h2>'
        f'<p>{html.escape(message)}</p><p><a style="color:#8da0ff" href="{target}">Back to Clippie</a></p></div>'
        f'<script>setTimeout(function(){{location.href="{target}"}},{1500 if ok else 8000})</script>',
        status_code=200 if ok else 400)


@router.get('/oauth/youtube/callback')
def youtube_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    saved = db().pop_state(state) if state else None
    if saved is None:
        return finish_page(False, 'This sign-in link expired or was already used. Start again from the Channels page.')
    if error or not code:
        return finish_page(False, f'Google reported: {error or "no authorisation code"}.')
    http = runtime.http_factory()
    try:
        client_id, client_secret = db().setting('youtube_client_id'), runtime.SECRETS.get('youtube_client_secret')
        tokens = youtube.exchange_code(http, client_id, client_secret, code, saved['verifier'], saved['redirect_uri'])
        if not tokens.get('refresh_token'):
            return finish_page(False, 'Google did not return a refresh token. Remove Clippie at myaccount.google.com/permissions and connect again.')
        client = youtube.YouTube(http, client_id, client_secret)
        client._access, client._expires = tokens['access_token'], time.time() + float(tokens.get('expires_in', 3600))
        channels = client.connect()
        if not channels:
            return finish_page(False, 'That Google account has no YouTube channel. Create one at youtube.com first.')
        info = channels[0]
        account = db().add_channel('youtube', 'youtube', info.display_name, info.external_id, info.scopes, None)
        ref = runtime.refresh_name(account['id'])
        runtime.SECRETS.set(ref, tokens['refresh_token'])
        db().add_channel('youtube', 'youtube', info.display_name, info.external_id, info.scopes, ref)
    except (PublishError, SecretError) as failure:
        return finish_page(False, str(failure))
    finally:
        http.close()
    sync.ensure_started(data_root())
    return finish_page(True, f'{info.display_name} can now receive Shorts from Clippie.')


class AggregatorBody(Strict):
    api_key: StrictStr | None = Field(default=None, max_length=300)


@router.post('/channels/aggregator/connect')
def connect_aggregator(body: AggregatorBody):
    store = db()
    try:
        if body.api_key:
            runtime.SECRETS.set('aggregator_key', body.api_key)
    except (SecretError, ValueError) as error:
        raise HTTPException(503 if isinstance(error, SecretError) else 422, str(error)) from None
    http = runtime.http_factory()
    try:
        found = runtime.aggregator_client(http, store).connect()
    except PublishError as error:
        raise upstream(error) from None
    finally:
        http.close()
    if not found:
        raise HTTPException(422, 'No TikTok or Instagram account is linked in the posting service yet. Link one there first.')
    accounts = [store.add_channel(c.platform, 'aggregator', c.display_name, c.external_id, c.scopes, 'aggregator_key')
                for c in found]
    sync.ensure_started(data_root())
    return {'channels': [channel_view(a) for a in accounts]}


@router.delete('/channels/{channel_id}', status_code=204)
def disconnect_channel(channel_id: str):
    store = db()
    try:
        channel = store.get_channel(channel_id)
    except KeyError:
        raise not_found('Channel not found.') from None
    if any(p['channel_id'] == channel_id and p['status'] in {'scheduled', 'uploading'} for p in store.posts()):
        raise HTTPException(409, 'This channel has posts waiting to publish. Delete or finish them first.')
    if channel['provider'] == 'youtube' and channel['token_ref']:
        refresh = runtime.SECRETS.get(channel['token_ref'])
        if refresh:
            http = runtime.http_factory()
            try:
                youtube.YouTube(http, '', '', refresh).revoke()
            finally:
                http.close()
        try:
            runtime.SECRETS.delete(channel['token_ref'])
        except SecretError as error:
            raise HTTPException(503, str(error)) from None
    store.delete_channel(channel_id)


# -- posts ------------------------------------------------------------------

class PostCreate(Strict):
    short_id: StrictStr = Field(min_length=1, max_length=100)
    revision_id: StrictStr = Field(min_length=1, max_length=100)
    platforms: list[StrictStr] = Field(min_length=1, max_length=6)
    title: StrictStr = Field(min_length=1, max_length=150)
    description: StrictStr = Field(default='', max_length=5000)
    hashtags: list[StrictStr] = Field(default_factory=list, max_length=30)
    scheduled_at: StrictStr | None = None
    draft: StrictBool = False

    @field_validator('title')
    @classmethod
    def titled(cls, value):
        if not value.strip():
            raise ValueError('Give the post a title.')
        return value.strip()


class PostPatch(Strict):
    title: StrictStr | None = Field(default=None, min_length=1, max_length=150)
    description: StrictStr | None = Field(default=None, max_length=5000)
    hashtags: list[StrictStr] | None = Field(default=None, max_length=30)
    scheduled_at: StrictStr | None = None
    clear_schedule: StrictBool = False


class PublishBody(Strict):
    scheduled_at: StrictStr | None = None


class UncertainResolution(Strict):
    checked_not_uploaded: StrictBool


def resolve_channel(name):
    store = db()
    try:
        return store.get_channel(name)
    except KeyError:
        pass
    matches = [c for c in store.channels() if c['platform'] == name]
    if not matches:
        label = {'youtube': 'YouTube', 'tiktok': 'TikTok', 'instagram': 'Instagram'}.get(name, name)
        raise HTTPException(422, f'No {label} channel is connected. Connect one on the Channels page.')
    return matches[0]


def find_export(short_id, revision_id):
    from . import final_quality
    best = None
    for item in studio_api.studio()._all('exports'):
        if item['clip_id'] == short_id and item['revision_id'] == revision_id and item['quality'] == RENDER_QUALITY:
            try:
                final_quality.verify_artifact(item, short_id=short_id, revision_id=revision_id)
            except final_quality.FinalQualityError:
                continue
            best = item
    return best


def start_export(short_id, revision_id):
    from .studio_api import ExportRequest
    from . import lifecycle, lifecycle_api
    try:
        collection = studio_api.get_collection(collections().get_short(short_id)['collection_id'])
    except (KeyError, HTTPException):
        collection = None
    if collection is not None and (reason := lifecycle.guard_reason(lifecycle_api.make_ctx(), collection)):
        raise HTTPException(409, reason)
    return studio_api.export_clip(short_id, ExportRequest(revision_id=revision_id, quality=RENDER_QUALITY))['job_id']


def ensure_post_render(post):
    """Explicit Publish/Retry upgrades legacy artifacts and clears old upload sessions."""
    from . import final_quality
    if post.get('external_id'):
        return {}
    if post.get('artifact_id'):
        try:
            item = studio_api.studio().get_export(post['artifact_id'])
            final_quality.verify_artifact(item, short_id=post['short_id'], revision_id=post['revision_id'])
            return {}
        except (KeyError, final_quality.FinalQualityError):
            pass
    elif post.get('render_job_id'):
        try:
            if studio_api.studio().get_job(post['render_job_id'])['status'] in {'queued', 'running'}:
                return {}
        except KeyError:
            pass
    artifact = find_export(post['short_id'], post['revision_id'])
    return {'artifact_id': artifact['id'] if artifact else None,
            'render_job_id': None if artifact else start_export(post['short_id'], post['revision_id']),
            'upload_session': None}


def revision_for(short_id, revision_id):
    try:
        revision = collections().get_revision(revision_id)
    except KeyError:
        raise not_found('Revision not found for this clip.') from None
    if revision['short_id'] != short_id:
        raise not_found('Revision not found for this clip.')
    return revision


@router.post('/posts', status_code=201)
def create_posts(body: PostCreate):
    short, collection = clip_context(body.short_id)
    revision = revision_for(short['id'], body.revision_id)
    when = parse_when(body.scheduled_at)
    channels = [resolve_channel(name) for name in dict.fromkeys(body.platforms)]
    export = find_export(short['id'], revision['id'])
    job_id = None if export or body.draft else start_export(short['id'], revision['id'])
    duration = (export or {}).get('duration') or studio_api.tl.revision_duration(revision['document'])
    posts = []
    for channel in channels:
        warnings = []
        if channel['platform'] == 'youtube':
            warnings = youtube.shorts_checks(duration, (export or {}).get('width'), (export or {}).get('height'))
        posts.append(db().create_post(
            platform=channel['platform'], channel_id=channel['id'], short_id=short['id'], revision_id=revision['id'],
            artifact_id=export['id'] if export else None, render_job_id=job_id,
            project_id=collection['id'], project_name=studio_api.project_name(collection),
            title=body.title, description=body.description, hashtags=[t.lstrip('#') for t in body.hashtags if t.strip('# ')],
            status='draft' if body.draft else 'scheduled', scheduled_at=when, duration=duration, warnings=warnings))
    if not body.draft:
        sync.ensure_started(data_root())
        sync.kick(data_root())
    return {'posts': [post_view(p) for p in posts]}


@router.get('/posts')
def list_posts(short_id: str | None = None, status: Literal['draft', 'scheduled', 'uploading', 'published', 'failed'] | None = None):
    cache = {}
    return {'posts': [post_view(p, cache=cache) for p in db().posts(short_id=short_id, status=status)]}


@router.get('/posts/{post_id}')
def read_post(post_id: str):
    return post_view(get_post(post_id))


def editable(post):
    if post['status'] not in {'draft', 'failed'} and not (post['status'] == 'scheduled' and not post['external_id']):
        raise HTTPException(409, 'This post is already on the platform. Edit it there.')


@router.patch('/posts/{post_id}')
def patch_post(post_id: str, body: PostPatch):
    post = get_post(post_id)
    editable(post)
    fields = {k: v for k, v in {'title': body.title, 'description': body.description}.items() if v is not None}
    if body.hashtags is not None:
        fields['hashtags'] = [t.lstrip('#') for t in body.hashtags if t.strip('# ')]
    if body.clear_schedule:
        fields['scheduled_at'] = None
    elif body.scheduled_at is not None:
        fields['scheduled_at'] = parse_when(body.scheduled_at)
    return post_view(db().update_post(post_id, **fields))


@router.post('/posts/{post_id}/publish')
def publish_post(post_id: str, body: PublishBody | None = None):
    post = get_post(post_id)
    if post['status'] != 'draft':
        raise HTTPException(409, 'Only drafts can be published.')
    fields = {'status': 'scheduled', 'error': None}
    fields.update(ensure_post_render(post))
    if body and body.scheduled_at is not None:
        fields['scheduled_at'] = parse_when(body.scheduled_at)
    updated = db().update_post(post_id, **fields)
    sync.ensure_started(data_root())
    sync.kick(data_root())
    return post_view(updated)


@router.post('/posts/{post_id}/retry')
def retry_post(post_id: str):
    post = get_post(post_id)
    if post['status'] != 'failed':
        raise HTTPException(409, 'Only failed posts can be retried.')
    if 'upload result is uncertain' in (post.get('error') or '').lower():
        raise HTTPException(409, 'Check the platform for this video before creating another upload.')
    fields = {'status': 'scheduled', 'error': None, 'attempts': 0, 'next_attempt_at': None}
    fields.update(ensure_post_render(post))
    if post['external_id']:
        fields['next_poll_at'] = time.time()
    updated = db().update_post(post_id, **fields)
    sync.ensure_started(data_root())
    sync.kick(data_root())
    return post_view(updated)


@router.post('/posts/{post_id}/resolve-uncertain')
def resolve_uncertain(post_id: str, body: UncertainResolution):
    post = get_post(post_id)
    if (post['status'] != 'failed' or post.get('external_id') or not body.checked_not_uploaded
            or 'upload result is uncertain' not in (post.get('error') or '').lower()):
        raise HTTPException(409, 'This post is not awaiting an upload check.')
    updated = db().update_post(post_id, upload_session=None,
                               error='Platform checked. You can retry the upload when ready.',
                               attempts=0, next_attempt_at=None)
    return post_view(updated)


@router.delete('/posts/{post_id}', status_code=204)
def delete_post(post_id: str):
    post = get_post(post_id)
    if post['status'] == 'uploading':
        raise HTTPException(409, 'This post is uploading. Try again in a moment.')
    if post['external_id'] and post['status'] == 'scheduled':
        raise HTTPException(409, 'This post is already uploaded and waiting to go live. Remove it on the platform first.')
    db().delete_post(post_id)


@router.post('/posts/{post_id}/sync')
def sync_post(post_id: str):
    post = get_post(post_id)
    if not post['external_id']:
        raise HTTPException(409, 'This post has not been uploaded yet.')
    db().update_post(post_id, next_poll_at=time.time())
    sync.service(data_root()).run_once()
    return post_view(get_post(post_id))


# -- metrics and retention --------------------------------------------------

@router.get('/posts/{post_id}/metrics')
def post_metrics(post_id: str):
    post = get_post(post_id)
    history = [{'fetched_at': iso(s['fetched_at']), 'source': s['source'], 'metrics': s['metrics']}
               for s in db().snapshots(post_id)]
    return {'post': post_view(post, with_metrics=False), 'latest': history[-1] if history else None, 'history': history}


def program_words(short_id, revision_id):
    """(words, duration) of the revision that was published, in program time."""
    try:
        short, collection = clip_context(short_id)
        revision = revision_for(short['id'], revision_id)
        sources = studio_api.revision_sources(collection, revision)
        timeline = studio_api.timeline_for(revision, collection, sources)
        return studio_api.engine.program_words(timeline, sources), studio_api.engine.duration(timeline)
    except (HTTPException, studio_api.engine.EngineUnavailable, studio_api.engine.InvalidTimeline, KeyError):
        return [], None


@router.get('/posts/{post_id}/retention')
def post_retention(post_id: str):
    post = get_post(post_id)
    curve = db().latest_retention(post_id)
    words, duration = program_words(post['short_id'], post['revision_id'])
    duration = duration or post['duration']
    points = curve['points'] if curve else []
    mapped = map_words(points, words, duration)
    latest = db().latest_snapshot(post_id)
    # organic | all | not_available_yet | None (never polled, or a platform without curves)
    audience = ((latest or {}).get('metrics') or {}).get('retention_audience')
    return {'post_id': post_id, 'revision_id': post['revision_id'], 'fetched_at': iso(curve['fetched_at']) if curve else None,
            'points': points, 'duration': duration, 'words': mapped['words'], 'drops': mapped['drops'],
            'audience': audience if isinstance(audience, str) else None}


COUNTED = ('views', 'likes', 'comments', 'shares', 'saves', 'subscribers_gained')


@router.get('/clips/{short_id}/performance')
def clip_performance(short_id: str):
    short, _ = clip_context(short_id)
    posts = [post_view(p) for p in db().posts(short_id=short['id'])]
    totals = {name: 0 for name in COUNTED}
    revisions = {}
    for post in posts:
        group = revisions.setdefault(post['revision_id'], {'revision_id': post['revision_id'], 'posts': 0, 'views': 0})
        group['posts'] += 1
        for name in COUNTED:
            value = (post['metrics'] or {}).get(name)
            if isinstance(value, (int, float)):
                totals[name] += value
                if name == 'views':
                    group['views'] += value
    return {'short_id': short['id'], 'current_revision_id': short.get('current_revision'), 'posts': posts,
            'totals': totals, 'revisions': list(revisions.values())}


# -- cloud ------------------------------------------------------------------

@router.post('/cloud/sync')
def cloud_sync():
    if not cloud.configured(db()):
        raise HTTPException(422, 'Add the cloud service URL and device token on the Channels page first.')
    http = runtime.http_factory()
    try:
        return cloud.sync(db(), http)
    except PublishError as error:
        raise upstream(error) from None
    finally:
        http.close()


@router.get('/status')
def status():
    store = db()
    counts = {}
    for post in store.posts():
        counts[post['status']] = counts.get(post['status'], 0) + 1
    return {'posts': counts, 'channels': len(store.channels()), 'cloud_configured': cloud.configured(store)}
