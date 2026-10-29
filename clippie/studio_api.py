"""Studio API (docs/v3/STUDIO_API.md): projects, clips, timeline edits, actions, export, brand kits.

A project is a collection and a clip is a short. Studio edits append collection revisions whose
document gains `timeline_v3`; the stored v2 `plan`/`timeline`, `editorial` and `technical` fields are
copied untouched, so every gate of the older collection API evaluates exactly as it did for the base
revision. The older preview/export render that v2 plan, not the studio edit; studio edits render
through `/api/studio/clips/{id}/export` only.
"""
import copy
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid
from typing import Literal

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from . import download, intake, media, studio_engine as engine, studio_media, studio_timeline as tl
from .studio_store import IdempotencyConflict, StudioStore, valid_id
from .paths import resource_root

router = APIRouter(prefix='/api/studio', tags=['studio'])
static_router = APIRouter(tags=['studio-static'])

STATIC = resource_root() / 'static' / 'studio'
FONTS_DIR = resource_root() / 'assets' / 'fonts'
FONT_FILE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.(?:ttf|otf|woff2?)$')
LAYOUTS = ['fill', 'fit', 'stack', 'screen_speaker']
MAX_UPLOAD_BYTES = 4 * 1024 ** 3
MAX_COUNT = 30
AUTO_COUNT = 12
MAX_ASSET_BYTES = 5 * 1024 ** 2
SOURCE_FRACTION = {'queued': 0.0, 'ingesting': 0.15, 'transcribing': 0.5, 'selecting': 0.85,
                   'exporting': 0.95, 'ready': 1.0, 'failed': 0.0}
STAGE_LABELS = {'prepare_source': 'Preparing video', 'map_source': 'Understanding video',
                'discover': 'Choosing distinct ideas', 'discover_more': 'Finding one more idea',
                'plan_short': 'Editing a clip', 'review_short': 'Checking the edit',
                'review_batch': 'Checking titles and hooks', 'repair_batch': 'Fixing titles and hooks',
                'compile_short': 'Building the edit',
                'render_preview': 'Making a preview', 'render_export': 'Exporting video',
                'apply_settings': 'Applying visual settings'}
ERROR_TEXT = {'provider_auth': 'The model key was rejected. Check the key in Settings.',
              'provider_quota': 'The model provider reports the quota is used up.',
              'provider_config': 'The model provider is not configured correctly.',
              'provider_network': 'The model provider could not be reached.',
              'provider_timeout': 'The model provider timed out.',
              'provider_deadline': 'Generation ran out of time. Try again.',
              'editor_invalid_output': 'A model answer did not pass validation, so that step stopped. '
                                       'Resume asks again; finished steps are kept.'}

_STORES, _LOCK = {}, threading.Lock()


# -- plumbing ---------------------------------------------------------------

def context():
    from . import app
    return app


def collections():
    from .collection_api import store
    return store()


def studio():
    root = str(context().STORE.root)
    with _LOCK:
        if root not in _STORES:
            _STORES[root] = StudioStore(root)
        return _STORES[root]


def data_root():
    return context().STORE.root


def guarded(function):
    """Map engine failures to HTTP: missing component 503, invalid timeline 422."""
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except engine.EngineUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except engine.InvalidTimeline as error:
            raise HTTPException(422, str(error)) from None
    return call


def not_found(text):
    return HTTPException(404, text)


def clean(error):
    return context().clean_error(error)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


# -- sources ----------------------------------------------------------------

def source_summary(job):
    from . import lifecycle
    path = job.get('preview_path') or job.get('source_path')
    ready = bool(job.get('duration') and path and Path(path).is_file())
    view = intake.progress_view(job)
    busy = job['status'] in intake.BUSY_STATUSES
    legacy = SOURCE_FRACTION.get(job['status'], 0.0)
    request = job.get('options') or {}
    return {'id': job['id'], 'title': job.get('studio_title') or job.get('title') or job['id'],
            'status': job['status'], 'progress': view['overall'] if job.get('stages') else legacy,
            'duration': job.get('duration') or job.get('expected_duration'),
            'thumbnail_url': f'/api/studio/sources/{job["id"]}/thumbnail' if ready else job.get('remote_thumbnail'),
            'media_url': f'/api/jobs/{job["id"]}/media' if job.get('preview_path') else None,
            'error': job.get('error'),
            'message': view['message'] if busy else (job.get('progress') if isinstance(job.get('progress'), str) else None),
            'kind': 'url' if intake.is_url(job.get('source') or '') else 'file',
            'stages': view['stages'], 'queue': view['queue'], 'stalled': view['stalled'],
            'stall_reason': view['stall_reason'], 'eta_seconds': view['eta_seconds'],
            'quality': request.get('quality'), 'range': request.get('range'),
            'cancellable': busy, 'retryable': job['status'] in {'failed', 'cancelled'},
            'freed': lifecycle.source_is_freed(job),
            'dismissable': job['status'] in {'failed', 'cancelled'}}


def get_job(source_id):
    if not valid_id(source_id):
        raise not_found('Source not found.')
    try:
        return context().STORE.get(source_id)
    except KeyError:
        raise not_found('Source not found.') from None


def start_source_job(source, title, quality=None, range_=None):
    app = context()
    options = {**({'quality': quality} if quality else {}), **({'range': range_} if range_ else {})}
    job = app.create_job(app.NewJob(source=source, transcriber='auto', **options))
    if title:
        job = app.STORE.update(job['id'], studio_title=title)
    return job


def clean_title(value):
    if value is None:
        return None
    value = value.strip()
    if len(value) > 200 or any(ord(c) < 32 for c in value):
        raise HTTPException(422, 'Title must be at most 200 characters without control characters.')
    return value or None


class SourceRange(Strict):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def ordered(self):
        if self.end <= self.start:
            raise ValueError('The range must end after it starts.')
        return self


class SourceRequest(Strict):
    url: StrictStr | None = Field(default=None, max_length=4096)
    path: StrictStr | None = Field(default=None, max_length=4096)
    title: StrictStr | None = Field(default=None, max_length=200)
    quality: Literal['standard', 'high', 'small'] | None = None
    range: SourceRange | None = None

    @model_validator(mode='after')
    def one_location(self):
        if bool(self.url) == bool(self.path):
            raise ValueError('Send exactly one of "url" or "path".')
        if self.url and not self.url.startswith(('https://', 'http://')):
            raise ValueError('The URL must start with http:// or https://.')
        if self.path and (self.quality or self.range):
            raise ValueError('Quality and range apply to web links only.')
        return self


class InspectRequest(Strict):
    url: StrictStr = Field(min_length=1, max_length=4096)

    @model_validator(mode='after')
    def web_link(self):
        if not self.url.strip().startswith(('https://', 'http://')):
            raise ValueError('The URL must start with http:// or https://.')
        return self


def video_only(content_type):
    return None if content_type.startswith('video/') else 'Upload a video file (video/* content type).'


def asset_type(content_type):
    if content_type == 'image/svg+xml':
        return 'SVG logos are not accepted. Upload a PNG or JPEG.'
    return None if content_type in {'image/png', 'image/jpeg'} else 'Upload a PNG or JPEG image.'


def upload_limit():
    try:
        return int(os.environ.get('CLIPPIE_STUDIO_MAX_UPLOAD_BYTES', MAX_UPLOAD_BYTES))
    except ValueError:
        return MAX_UPLOAD_BYTES


@router.post('/uploads', status_code=202)
async def upload_source(request: Request):
    directory = data_root() / 'uploads' / uuid.uuid4().hex
    try:
        fields, file = await studio_media.receive_multipart(request, directory, upload_limit(), video_only)
        if file is None or not file['size']:
            raise studio_media.UploadError(422, 'Send the video in the "file" field.')
        try:
            await to_thread.run_sync(media.probe, file['path'])
        except (RuntimeError, OSError):
            raise studio_media.UploadError(422, 'That file is not a readable video.') from None
        title = clean_title(fields.get('title')) or Path(file['filename']).stem[:200] or None
        job = await to_thread.run_sync(start_source_job, str(file['path']), title)
    except studio_media.UploadError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(error.status, error.detail) from None
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    studio().add_upload({'id': job['id'], 'path': str(file['path']), 'filename': file['filename'],
                         'content_type': file['content_type'], 'size': file['size']})
    return {'source_id': job['id'], 'status': job['status']}


@router.post('/sources', status_code=202)
def add_source(body: SourceRequest):
    job = start_source_job((body.url or body.path).strip(), clean_title(body.title), body.quality,
                           body.range.model_dump() if body.range else None)
    return {'source_id': job['id'], 'status': job['status']}


@router.post('/sources/inspect')
def inspect_source(body: InspectRequest):
    """Pre-flight for a pasted link: title, length, thumbnail, size per quality, and how long transcription should take."""
    try:
        meta = download.inspect(body.url.strip())
    except download.InspectError as error:
        raise HTTPException(422, str(error)) from None
    except Exception as error:
        raise HTTPException(422, f'Could not read that link: {clean(error)[-300:]}') from None
    stats = intake.Stats(data_root())
    speed = stats.get('download_bytes_per_second')
    sizes = meta.pop('download_bytes')
    provider = media.resolve_provider('auto')
    return {'title': meta['title'], 'duration': meta['duration'], 'thumbnail_url': meta['thumbnail_url'],
            'uploader': meta['uploader'], 'options': meta['options'],
            'estimates': {'download_bytes': sizes,
                          'download_seconds': {k: round(v / speed) if v and speed else None for k, v in sizes.items()},
                          'transcribe_seconds': intake.expected_transcribe_seconds(stats, provider, meta['duration'])}}


@router.post('/sources/{source_id}/cancel')
def cancel_source(source_id: str):
    get_job(source_id)
    return source_summary(context().cancel_job(source_id))


@router.post('/sources/{source_id}/retry', status_code=202)
def retry_source(source_id: str):
    get_job(source_id)
    return source_summary(context().retry_job(source_id))


@router.get('/sources')
def list_sources():
    return [source_summary(job) for job in context().STORE.all()]


@router.get('/sources/{source_id}/thumbnail')
def source_thumbnail(source_id: str):
    job = get_job(source_id)
    path = job.get('preview_path') or job.get('source_path')
    if not job.get('duration') or not path:
        raise not_found('Thumbnail is not available yet.')
    image = studio_media.thumbnail(path, job['duration'] * 0.1, data_root() / 'studio' / 'thumbnails', f'src-{source_id}')
    return image_response(image)


def image_response(image):
    if image is None:
        raise not_found('Thumbnail is not available.')
    return FileResponse(image, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})


# -- clip and project read models ------------------------------------------

def clip_context(clip_id):
    if not valid_id(clip_id):
        raise not_found('Clip not found.')
    try:
        short = collections().get_short(clip_id)
        return short, collections().get_collection(short['collection_id'])
    except KeyError:
        raise not_found('Clip not found.') from None


def short_revision(short):
    if not short.get('current_revision'):
        raise HTTPException(409, 'This clip has no edit yet.')
    return collections().get_revision(short['current_revision'])


def project_jobs(collection):
    jobs = {}
    for source_id in collection['source_ids']:
        try:
            jobs[source_id] = context().STORE.get(source_id)
        except KeyError:
            continue
    return jobs


def revision_sources(collection, revision):
    """Project source jobs, using the revision's saved transcript version when it exists."""
    jobs = project_jobs(collection)
    try:
        from .collection_generation import _revision_sources
        jobs.update(_revision_sources(collections(), context().STORE, revision))
    except (KeyError, ValueError, OSError, TypeError, AttributeError):
        pass
    return jobs


def project_defaults(timeline, collection, sources):
    """Read-time defaults from project options (brand kit, else template); never stored by a read."""
    options = studio().project_options(collection['id'])
    changed = None
    if options.get('brand_kit_id'):
        try:
            changed = tl.apply_brand_kit(timeline, studio().get_kit(options['brand_kit_id']))
        except KeyError:
            changed = None  # the kit was deleted: fall back to the template
    if changed is None and options.get('template_id'):
        changed = copy.deepcopy(timeline)
        changed['captions']['template_id'] = options['template_id']
    if changed is None:
        return timeline
    try:
        return engine.validate(changed, sources)
    except engine.InvalidTimeline:
        return timeline


def timeline_for(revision, collection, sources):
    document = revision['document']
    stored = tl.stored_v3(document)
    if stored:
        return stored
    return project_defaults(engine.from_v2(copy.deepcopy(document['timeline']), sources), collection, sources)


def revision_title(short, revision):
    plan = revision['document'].get('plan') or {}
    return plan.get('title') or short.get('metadata', {}).get('title') or 'Untitled clip'


def preview_url(short, revision):
    export = studio().latest_export(short['id'], revision['id'])
    if export:
        return f'/api/studio/files/{export["id"]}/video'
    previews = [a for a in collections().list_artifacts(short['id'])
                if a['kind'] == 'preview' and a['revision_id'] == revision['id']]
    return f'/api/studio/files/{previews[-1]["id"]}/video' if previews else None


def clip_work_state(short, tasks, collection=None):
    """Explain this clip's own work and its relevant batch prerequisites.

    Batch repair/review tasks have no short_id. Only the short's generation and
    discovery branch may block it; an unrelated or historical batch must not.
    """
    if short.get('current_revision'):
        return {'status': 'ready'}
    if short.get('held_revision'):
        return {'status': 'needs_attention', 'status_message': 'Open this clip to inspect its editorial review.'}
    if collection and collection['status'] == 'cancelled':
        return {'status': 'cancelled', 'status_message': 'Generation was cancelled.'}
    run_id = (short.get('metadata') or {}).get('run_id')
    if collection and run_id and run_id != collection.get('current_run'):
        return {'status': 'paused', 'status_message': 'This clip belongs to an earlier generation.'}
    own = [task for task in tasks if not run_id or task.get('payload', {}).get('run_id') == run_id]
    mine = [task for task in own if task.get('short_id') == short['id']]
    if any(task['status'] in {'queued', 'active'} for task in mine):
        return {'status': 'processing', 'status_message': 'This clip is being prepared.'}
    stopped = {'failed', 'held_budget', 'held_dependency', 'cancelled'}
    blocked = next((task for task in reversed(mine) if task['status'] in stopped), None)
    candidate = short.get('candidate_id')
    by_id = {task['id']: task for task in own}
    more_task = (short.get('metadata') or {}).get('more_task')
    batches = []
    for task in own:
        if task.get('short_id') or task['stage'] not in {'review_batch', 'repair_batch'}:
            continue
        payload = task.get('payload') or {}
        discovery = payload.get('discover_task')
        if more_task and discovery != more_task:
            continue
        if not more_task and discovery and by_id.get(discovery, {}).get('stage') == 'discover_more':
            continue
        prior_review = by_id.get(payload.get('review_task'), {})
        verdict = ((prior_review.get('result') or {}).get('results') or {}).get(candidate, {})
        if task['stage'] == 'repair_batch' and verdict.get('status') == 'pass':
            continue
        repair = by_id.get(payload.get('repair_task'), {})
        if repair.get('status') == 'completed' and candidate not in (repair.get('result') or {}).get('repairs', {}):
            continue
        batches.append(task)
    if blocked is None:
        blocked = next((task for task in reversed(batches) if task['status'] in stopped), None)
    if blocked:
        cancelled = blocked['status'] == 'cancelled'
        return {'status': 'cancelled' if cancelled else ('failed' if blocked in mine else 'paused'),
                'status_message': ('Generation was cancelled.' if cancelled else
                    f"Waiting for {STAGE_LABELS.get(blocked['stage'], 'generation').lower()}. This step has stopped."),
                **({'status_action': {'id': 'resume_project', 'label': 'Resume project'}}
                   if not cancelled and resumable([blocked]) else {})}
    if any(task['status'] in {'queued', 'active'} for task in batches):
        return {'status': 'processing', 'status_message': 'Waiting for the shared title and hook checks.'}
    return {'status': 'waiting', 'status_message': 'Waiting for the next generation step.'}


def clip_status(short, tasks, collection=None):
    return clip_work_state(short, tasks, collection)['status']


def clip_score(short, document):
    """(score, reasons): the revision's own score (real duration), else the discovery score on the short."""
    saved = (document or {}).get('score') or {}
    meta = short.get('metadata') or {}
    value = saved.get('score', meta.get('score'))
    reasons = saved.get('score_reasons', meta.get('score_reasons')) or []
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, []
    return float(value), [str(reason) for reason in reasons]


def clip_post_copy(short, plan=None):
    """Suggested public copy for publishing: the revision's plan first, then the discovery metadata on the short."""
    plan, meta = plan or {}, short.get('metadata') or {}
    pick = lambda key: plan.get(key) if plan.get(key) else meta.get(key)
    tags = pick('hashtags')
    return {'title': pick('post_title') or '', 'description': pick('post_description') or '',
            'hashtags': [str(t) for t in tags] if isinstance(tags, list) else [], 'hook_line': pick('hook_line') or ''}


def clip_summary(short, tasks, collection=None):
    summary = {'id': short['id'], 'title': short.get('metadata', {}).get('title') or 'Untitled clip', 'score': None,
               'score_reasons': [], 'duration': None,
               'revision_id': short.get('current_revision'), 'thumbnail_url': None, 'preview_url': None,
               **clip_work_state(short, tasks, collection), 'format': short.get('metadata', {}).get('edit_format')}
    document = None
    if short.get('current_revision'):
        revision = collections().get_revision(short['current_revision'])
        document = revision['document']
        summary.update(title=revision_title(short, revision), duration=tl.revision_duration(document),
                       thumbnail_url=f'/api/studio/clips/{short["id"]}/thumbnail',
                       preview_url=preview_url(short, revision),
                       format=(document.get('plan') or {}).get('edit_format') or summary['format'])
    if short.get('held_revision'):
        held = collections().get_revision(short['held_revision'])
        held_document = held['document']
        held_plan = held_document.get('plan') or {}
        review = held_document.get('editorial') or {}
        summary['held_review'] = {'revision_id': held['id'], 'title': held_plan.get('title') or summary['title'],
                                  'hook_line': held_plan.get('hook_line') or '',
                                  'status': review.get('status', 'needs_revision'),
                                  'issues': [{key: issue.get(key) for key in ('category', 'severity', 'detail', 'unit_ids')}
                                             for issue in review.get('issues', []) if isinstance(issue, dict)]}
        if not short.get('current_revision'):
            summary['title'] = summary['held_review']['title']
            summary['status_message'] = 'Inspect the editorial review below. This edit needs revision before it can be used.'
    summary['score'], summary['score_reasons'] = clip_score(short, document)
    summary['post'] = clip_post_copy(short, (document or {}).get('plan'))
    return summary


def sorted_clips(summaries):
    # Highest score first; unscored clips and ties keep creation order.
    ranked = sorted(enumerate(summaries), key=lambda item: (item[1]['score'] is None, -(item[1]['score'] or 0), item[0]))
    return [item[1] for item in ranked]


def source_entry(job):
    info = job.get('source_info') or {}
    return {'title': job.get('studio_title') or job.get('title') or job['id'], 'duration': job.get('duration'),
            'media_url': f'/api/jobs/{job["id"]}/media' if job.get('preview_path') else None,
            'width': info.get('width'), 'height': info.get('height')}


def iso_revisions(short_id):
    items = collections().list_revisions(short_id)
    return [{'id': r['id'], 'created_at': tl.iso(r['created_at']), 'origin': r['document'].get('origin', 'generated')}
            for r in reversed(items)]


@router.get('/clips/{clip_id}')
@guarded
def clip_detail(clip_id: str):
    short, collection = clip_context(clip_id)
    revision = short_revision(short)
    sources = revision_sources(collection, revision)
    timeline = timeline_for(revision, collection, sources)
    kept = engine.program_words(timeline, sources)
    tasks = collections().list_tasks(collection['id'])
    summary = clip_summary(short, current_project_tasks(collection, tasks), collection)
    if tl.stored_v3(revision['document']) is None:
        summary['duration'] = engine.duration(timeline)
    return {'clip': summary, 'project_id': collection['id'], 'revision_id': revision['id'], 'timeline': timeline,
            'words': tl.words_payload(timeline, sources, kept),
            'sources': {sid: source_entry(job) for sid, job in sources.items()},
            'revisions': iso_revisions(short['id'])}


@router.get('/clips/{clip_id}/thumbnail')
def clip_thumbnail(clip_id: str):
    short, _ = clip_context(clip_id)
    revision = short_revision(short)
    source_id, seconds = tl.first_source_time(revision['document'])
    try:
        job = context().STORE.get(source_id)
    except KeyError:
        raise not_found('Thumbnail is not available.') from None
    path = job.get('preview_path') if job.get('preview_path') and Path(job['preview_path']).is_file() else job.get('source_path')
    if not path:
        raise not_found('Thumbnail is not available.')
    at = min(seconds + 0.3, max(0.0, (job.get('duration') or seconds + 0.3) - 0.1))
    return image_response(studio_media.thumbnail(path, at, data_root() / 'studio' / 'thumbnails', f'clip-{clip_id}'))


# -- timeline writes --------------------------------------------------------

class TimelineWrite(Strict):
    base_revision: StrictStr = Field(min_length=1, max_length=100)
    timeline: dict
    request_id: StrictStr = Field(min_length=1, max_length=100)


class ActionBase(Strict):
    base_revision: StrictStr = Field(min_length=1, max_length=100)
    request_id: StrictStr = Field(min_length=1, max_length=100)


class FillerAction(ActionBase):
    silence_min: float = Field(default=0.6, ge=0.1, le=5, allow_inf_nan=False)


class BrandKitAction(ActionBase):
    brand_kit_id: StrictStr = Field(min_length=1, max_length=100)


def fingerprint(operation, **params):
    return hashlib.sha256(json.dumps([operation, params], sort_keys=True, default=str).encode()).hexdigest()


def replayed(clip_id, request_id, print_):
    try:
        found = studio().replay(clip_id, request_id, print_)
    except IdempotencyConflict as error:
        raise HTTPException(409, str(error)) from None
    return JSONResponse(found[1], status_code=found[0]) if found else None


def check_base(short, base_revision):
    if short.get('current_revision') != base_revision:
        raise HTTPException(409, f'base_revision is not current (current: {short.get("current_revision")}). Reload the clip.')


def append_studio_revision(short, base, timeline, sources, action):
    """Validate, then append a promoted revision. Returns (revision id, canonical timeline)."""
    canonical = engine.validate(timeline, sources)
    document = tl.revision_document(base['document'], canonical, action)
    try:
        saved = collections().append_revision(short['id'], document, expected_revision=base['id'])
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return saved['id'], canonical


def write_response(revision_id, timeline, **extra):
    return {'revision_id': revision_id, 'timeline': timeline, **extra}


def edit_in_place(clip_id, request_id, print_, base_revision, produce):
    """Shared PUT/action flow: replay, stale check, produce a new timeline, append, remember."""
    short, collection = clip_context(clip_id)
    with studio().lock:
        again = replayed(clip_id, request_id, print_)
        if again:
            return again
        short = collections().get_short(clip_id)
        check_base(short, base_revision)
        base = short_revision(short)
        sources = revision_sources(collection, base)
        current = timeline_for(base, collection, sources)
        timeline, action, changed = produce(current, sources)
        if changed:
            revision_id, canonical = append_studio_revision(short, base, timeline, sources, action)
        else:
            revision_id, canonical = base['id'], current
        body = write_response(revision_id, canonical, **({'changed': changed} if action != 'edit' else {}))
        studio().remember(clip_id, request_id, print_, 200, body)
    return JSONResponse(body)


@router.put('/clips/{clip_id}/timeline')
@guarded
def put_timeline(clip_id: str, body: TimelineWrite):
    print_ = fingerprint('put_timeline', base=body.base_revision, timeline=body.timeline)
    return edit_in_place(clip_id, body.request_id, print_, body.base_revision,
                         lambda current, sources: (follow_cuts(current, body.timeline, sources), 'edit', True))


def follow_cuts(current, incoming, sources):
    """Accept an edit of the speech as sent. If it only changed segments/cuts and left shots, B-roll
    and hooks as they were, those program-time items no longer fit: retime them (what a client would
    otherwise have to compute) and use that when it validates."""
    try:
        engine.validate(incoming, sources)
        return incoming
    except engine.InvalidTimeline:
        untouched = all(incoming.get(key) == current.get(key) for key in ('shots', 'broll')) and \
            [o for o in incoming.get('overlays') or [] if o.get('kind') == 'hook'] == \
            [o for o in current.get('overlays') or [] if o.get('kind') == 'hook']
        if not untouched or not isinstance(incoming.get('segments'), list) or incoming['segments'] == current['segments']:
            raise
        try:
            moved = tl.retime_program(current, incoming, engine.duration(incoming))
            engine.validate(moved, sources)
            return moved
        except (engine.InvalidTimeline, KeyError, TypeError, ValueError, IndexError):
            pass
        raise


@router.post('/clips/{clip_id}/actions/remove-fillers')
@guarded
def remove_fillers(clip_id: str, body: FillerAction):
    print_ = fingerprint('remove_fillers', base=body.base_revision, silence_min=body.silence_min)

    def produce(current, sources):
        cut, changed = tl.add_filler_cuts(current, sources, engine.detect_fillers, body.silence_min)
        return (carry_program(current, cut, sources) if changed else cut), 'remove_fillers', changed
    return edit_in_place(clip_id, body.request_id, print_, body.base_revision, produce)


def carry_program(old, new, sources):
    """Retime shots/B-roll/hooks to the new cuts; drop shots if the engine still rejects the result."""
    total = engine.duration(new)
    moved = tl.retime_program(old, new, total)
    try:
        engine.validate(moved, sources)
        return moved
    except engine.InvalidTimeline:
        moved['shots'], moved['broll'] = [], []
        return moved


@router.post('/clips/{clip_id}/actions/apply-brand-kit')
@guarded
def apply_brand_kit(clip_id: str, body: BrandKitAction):
    kit = get_kit(body.brand_kit_id)
    print_ = fingerprint('apply_brand_kit', base=body.base_revision, kit=kit['id'], updated=kit['updated_at'])
    check_logo(kit.get('logo_asset_id'))
    return edit_in_place(clip_id, body.request_id, print_, body.base_revision,
                         lambda current, sources: (tl.apply_brand_kit(current, kit), 'apply_brand_kit', True))


# -- jobs -------------------------------------------------------------------

def job_view(job):
    return {**{key: job[key] for key in ('id', 'kind', 'status', 'progress', 'error', 'result')},
            **({'message': job['message']} if job.get('message') else {})}


@router.get('/jobs/{job_id}')
def get_job_status(job_id: str):
    if not valid_id(job_id):
        raise not_found('Job not found.')
    try:
        return job_view(studio().get_job(job_id))
    except KeyError:
        raise not_found('Job not found.') from None


class Progress:
    """Throttled progress writer handed to long renders and analyses."""

    def __init__(self, store, job_id):
        self.store, self.job_id, self.last = store, job_id, 0.0

    def __call__(self, value=None, *_):
        if isinstance(value, dict):
            value = value.get('fraction')
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        value = min(max(float(value), 0.0), 0.99)
        if value - self.last >= 0.02:
            self.last = value
            self.store.update_job(self.job_id, progress=round(value, 3))


def submit_job(store, job, work, *args):
    """Run `work(store, job_id, *args)` on the app's single worker; failures end the job as failed."""
    def run():
        store.update_job(job['id'], status='running')
        try:
            work(store, job['id'], *args)
        except Exception as error:
            store.update_job(job['id'], status='failed', error=clean(error))
        finally:
            from . import job_locks
            job_locks.release(store.root, job['id'])
    try:
        future = context().WORKER.submit(run)
        def release_cancelled(future):
            if future.cancelled():
                from . import job_locks
                job_locks.release(store.root, job['id'])
        if future is not None:
            future.add_done_callback(release_cancelled)
    except RuntimeError:
        store.update_job(job['id'], status='failed', error='The app is shutting down. Try again.')
        from . import job_locks
        job_locks.release(store.root, job['id'])


def recover_jobs():
    """Resume abandoned exports; a live process retains its OS-backed job lock."""
    from . import final_quality, job_locks
    store = studio()
    for old in store.list_jobs():
        if old.get('status') not in ('queued', 'running') or not job_locks.acquire(store.root, old['id']):
            continue
        try:
            job = store.get_job(old['id'])
            if job.get('status') not in ('queued', 'running'):
                job_locks.release(store.root, job['id'])
                continue
            payload = job.get('payload') or {}
            revision_id = payload.get('revision_id')
            if job.get('kind') != 'export' or not revision_id or not job.get('clip_id'):
                store.update_job(job['id'], status='failed', error='This interrupted task needs to be started again.')
                job_locks.release(store.root, job['id'])
                continue
            # A crash may have happened after the artifact was committed but before the job was completed.
            found = None
            for artifact in store._all('exports'):
                if (artifact.get('clip_id') == job['clip_id'] and artifact.get('revision_id') == revision_id
                        and job['id'] in Path(artifact.get('video_path') or '').parts
                        and payload.get('quality') == '1080p'):
                    try:
                        final_quality.verify_artifact(artifact, short_id=job['clip_id'], revision_id=revision_id)
                        found = artifact
                    except final_quality.FinalQualityError:
                        pass
            if found:
                store.update_job(job['id'], status='done', progress=1.0, error=None, message='Recovered completed export',
                                 result={'video_url': f'/api/studio/files/{found["id"]}/video',
                                         'srt_url': f'/api/studio/files/{found["id"]}/subtitles',
                                         'duration': found.get('duration')})
                job_locks.release(store.root, job['id'])
                continue
            attempt = int(job.get('attempt', 1)) + 1
            if attempt > 3:
                raise ValueError('Export was interrupted repeatedly. Check the source and start it again.')
            short, collection = clip_context(job['clip_id'])
            revision = collections().get_revision(revision_id)
            if revision['short_id'] != short['id']:
                raise ValueError('The saved edit for this export is no longer available.')
            sources = revision_sources(collection, revision)
            timeline = engine.validate(timeline_for(revision, collection, sources), sources)
            assets = asset_paths(timeline)
            if not payload.get('input_fingerprint') or payload['input_fingerprint'] != export_input_fingerprint(timeline, sources, assets):
                raise ValueError('The source or edit changed since this export began. Start the export again after review.')
            store.update_job(job['id'], status='queued', progress=0.0, error=None, attempt=attempt,
                             message='Recovering interrupted export')
            submit_job(store, job, run_export, job['clip_id'], revision_id, payload.get('quality'), timeline, sources, assets)
        except Exception as error:
            store.update_job(old['id'], status='failed', error=clean(error), message='Recovery needs attention')
            job_locks.release(store.root, old['id'])


def run_reframe(store, job_id, clip_id, base_id, timeline, sources):
    store.update_job(job_id, progress=0.05)
    shots = engine.auto_shots(timeline, sources, cache_dir=data_root() / 'analysis-v3')
    result = {**copy.deepcopy(timeline), 'shots': shots}
    with store.lock:
        short = collections().get_short(clip_id)
        if short.get('current_revision') != base_id:
            raise ValueError('The clip changed while it was being analysed. Run auto-reframe again.')
        base = collections().get_revision(base_id)
        try:
            revision_id, canonical = append_studio_revision(short, base, result, sources, 'auto_reframe')
        except HTTPException as error:
            raise ValueError(error.detail) from None
    store.update_job(job_id, status='done', progress=1.0, result=write_response(revision_id, canonical))


@router.post('/clips/{clip_id}/actions/auto-reframe', status_code=202)
@guarded
def auto_reframe(clip_id: str, body: ActionBase):
    short, collection = clip_context(clip_id)
    engine.module('reframe')
    print_ = fingerprint('auto_reframe', base=body.base_revision)
    with studio().lock:
        again = replayed(clip_id, body.request_id, print_)
        if again:
            return again
        check_base(collections().get_short(clip_id), body.base_revision)
        base = short_revision(short)
        sources = revision_sources(collection, base)
        timeline = timeline_for(base, collection, sources)
        job = studio().create_job('auto_reframe', clip_id, {'base_revision': base['id']})
        answer = {'job_id': job['id']}
        studio().remember(clip_id, body.request_id, print_, 202, answer)
    submit_job(studio(), job, run_reframe, clip_id, base['id'], timeline, sources)
    return JSONResponse(answer, status_code=202)


# -- export -----------------------------------------------------------------

class ExportRequest(Strict):
    revision_id: StrictStr = Field(min_length=1, max_length=100)
    quality: Literal['preview', '1080p']


def asset_paths(timeline):
    assets = {}
    for overlay in timeline.get('overlays') or []:
        if overlay['kind'] == 'logo':
            try:
                assets[overlay['asset_id']] = asset_file(studio().get_asset(overlay['asset_id']))
            except (KeyError, HTTPException):
                raise HTTPException(422, 'The logo image is no longer available. Pick another logo.') from None
    return assets


def contained(path, directory):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(directory).resolve()) or not resolved.is_file():
        raise ValueError('The renderer produced a file outside its output folder.')
    return resolved


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def export_input_fingerprint(timeline, sources, assets):
    """Bind recovery to the original render inputs without storing transcript text in job diagnostics."""
    used = {item['source_id'] for key in ('segments', 'broll') for item in timeline.get(key, [])}
    source_proof = {}
    for source_id in sorted(used):
        source = sources[source_id]
        path = Path(source['source_path']).resolve()
        stat = path.stat()
        source_proof[source_id] = {'path': str(path), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                                   'master_sha256': (source.get('provenance') or {}).get('master_sha256'),
                                   'words': source.get('words', [])}
    asset_proof = {name: sha256_of(path) for name, path in sorted(assets.items())}
    value = {'timeline': timeline, 'sources': source_proof, 'assets': asset_proof}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def run_export(store, job_id, clip_id, revision_id, quality, timeline, sources, assets):
    from . import final_quality
    expected = (store.get_job(job_id).get('payload') or {}).get('input_fingerprint')
    if expected and expected != export_input_fingerprint(timeline, sources, assets):
        raise ValueError('The source or edit changed while the export was waiting. Start the export again after review.')
    directory = data_root() / 'collection-artifacts' / 'studio' / revision_id / job_id
    attempt = store.get_job(job_id).get('attempt', 1)
    if attempt > 1:
        directory = directory / f'attempt-{attempt}'
    directory.mkdir(parents=True, exist_ok=False)
    source_quality = None
    if quality == '1080p':
        timeline = final_quality.final_timeline(timeline)
        sources, source_quality = final_quality.prepare_sources(timeline, sources, data_root(),
            progress=lambda message: store.update_job(job_id, message=message))
        store.update_job(job_id, message='Rendering final HD video')
    result = engine.render(timeline, sources, directory, assets=assets,
                           progress=Progress(store, job_id), preview=quality == 'preview')
    video, srt = contained(result['video_path'], directory), contained(result['srt_path'], directory)
    manifest = result.get('manifest_path')
    record = {
        'id': uuid.uuid4().hex, 'clip_id': clip_id, 'revision_id': revision_id, 'quality': quality,
        'video_path': str(video), 'srt_path': str(srt),
        'manifest_path': str(contained(manifest, directory)) if manifest else None,
        'duration': result.get('duration'), 'width': result.get('width'), 'height': result.get('height'),
        'sha256': sha256_of(video), 'warnings': result.get('warnings', [])}
    if source_quality is not None:
        final_quality.verify_sources(source_quality)
        record['final_quality'] = {'policy': final_quality.POLICY, 'sources': source_quality,
                                   'duration': engine.duration(timeline),
                                   'canvas': {'width': timeline['canvas']['width'], 'height': timeline['canvas']['height']}}
        final_quality.verify_artifact(record, short_id=clip_id, revision_id=revision_id)
    artifact = store.add_export(record)
    store.update_job(job_id, status='done', progress=1.0, error=None, result={
        'video_url': f'/api/studio/files/{artifact["id"]}/video',
        'srt_url': f'/api/studio/files/{artifact["id"]}/subtitles', 'duration': result.get('duration')})


@router.post('/clips/{clip_id}/export', status_code=202)
@guarded
def export_clip(clip_id: str, body: ExportRequest):
    short, collection = clip_context(clip_id)
    engine.module('render_v3')
    if not valid_id(body.revision_id):
        raise not_found('Revision not found for this clip.')
    try:
        revision = collections().get_revision(body.revision_id)
    except KeyError:
        raise not_found('Revision not found for this clip.') from None
    if revision['short_id'] != clip_id:
        raise not_found('Revision not found for this clip.')
    if revision['document'].get('inspection_only') or short.get('held_revision') == revision['id']:
        raise HTTPException(409, 'This revision is held for editorial review and cannot be exported.')
    sources = revision_sources(collection, revision)
    timeline = engine.validate(timeline_for(revision, collection, sources), sources)
    assets = asset_paths(timeline)
    job = studio().create_job('export', clip_id, {'revision_id': revision['id'], 'quality': body.quality,
                                                   'input_fingerprint': export_input_fingerprint(timeline, sources, assets)})
    submit_job(studio(), job, run_export, clip_id, revision['id'], body.quality, timeline, sources, assets)
    return {'job_id': job['id']}


def artifact_path(artifact_id, kind):
    if not valid_id(artifact_id):
        raise not_found('Artifact not found.')
    root = (data_root() / 'collection-artifacts').resolve()
    try:
        document = studio().get_export(artifact_id)
    except KeyError:
        try:
            artifact = collections().get_artifact(artifact_id)
        except KeyError:
            raise not_found('Artifact not found.') from None
        if artifact['kind'] not in {'preview', 'export'}:
            raise not_found('Artifact not found.')
        document = artifact['document']
    path = document.get('video_path' if kind == 'video' else 'srt_path')
    resolved = Path(path).resolve() if path else None
    if resolved is None or not resolved.is_relative_to(root) or not resolved.is_file():
        raise not_found('Saved file is unavailable.')
    return resolved


@router.get('/files/{artifact_id}/{kind}')
def artifact_file(artifact_id: str, kind: Literal['video', 'subtitles']):
    path = artifact_path(artifact_id, kind)
    return FileResponse(path, media_type='video/mp4' if kind == 'video' else 'text/plain')


# -- templates and brand kits -----------------------------------------------

@router.get('/templates')
@guarded
def templates():
    registry = engine.load_templates()
    hooks = [{'id': name, **style} if 'id' not in style else style for name, style in registry['hooks'].items()]
    captions = [{'id': name, **item} if 'id' not in item else item for name, item in registry['captions'].items()]
    return {'captions': captions, 'hooks': hooks, 'layouts': LAYOUTS, 'fonts_base_url': '/static/fonts/'}


class ProgressBar(Strict):
    enabled: StrictBool
    color: StrictStr = Field(pattern=r'^#[0-9A-Fa-f]{6}$')


class BrandKitBody(Strict):
    name: StrictStr = Field(min_length=1, max_length=80)
    template_id: StrictStr = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    overrides: dict = Field(default_factory=dict)
    hook_style: StrictStr = Field(default='hook_box', pattern=r'^[A-Za-z0-9_-]{1,64}$')
    progress_bar: ProgressBar = Field(default_factory=lambda: ProgressBar(enabled=False, color='#FFD400'))
    logo_asset_id: StrictStr | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def named(self):
        self.name = self.name.strip()
        if not self.name or any(ord(c) < 32 for c in self.name):
            raise ValueError('Name must be text without control characters.')
        return self


def kit_view(kit):
    return {**kit, 'created_at': tl.iso(kit['created_at']), 'updated_at': tl.iso(kit['updated_at']),
            'logo_url': f'/api/studio/brand-assets/{kit["logo_asset_id"]}' if kit.get('logo_asset_id') else None}


def get_kit(kit_id):
    if not valid_id(kit_id):
        raise not_found('Brand kit not found.')
    try:
        return studio().get_kit(kit_id)
    except KeyError:
        raise not_found('Brand kit not found.') from None


def check_logo(asset_id):
    if asset_id is None:
        return
    if not valid_id(asset_id):
        raise HTTPException(422, 'Unknown logo image.')
    try:
        studio().get_asset(asset_id)
    except KeyError:
        raise HTTPException(422, 'Unknown logo image.') from None


def check_kit(body):
    check_logo(body.logo_asset_id)
    try:
        registry = engine.load_templates()
    except engine.EngineUnavailable:
        return
    if body.template_id not in registry['captions']:
        raise HTTPException(422, f'Unknown caption template "{body.template_id}".')
    if body.hook_style not in registry['hooks']:
        raise HTTPException(422, f'Unknown hook style "{body.hook_style}".')
    try:
        engine.resolve_caption(body.template_id, body.overrides)
    except ValueError as error:
        raise HTTPException(422, f'Invalid caption overrides: {error}') from None


@router.get('/brand-kits')
def list_brand_kits():
    return [kit_view(kit) for kit in studio().list_kits()]


@router.post('/brand-kits', status_code=201)
def create_brand_kit(body: BrandKitBody):
    check_kit(body)
    return kit_view(studio().create_kit(body.model_dump()))


@router.put('/brand-kits/{kit_id}')
def update_brand_kit(kit_id: str, body: BrandKitBody):
    get_kit(kit_id)
    check_kit(body)
    return kit_view(studio().update_kit(kit_id, body.model_dump()))


@router.delete('/brand-kits/{kit_id}', status_code=204)
def delete_brand_kit(kit_id: str):
    get_kit(kit_id)
    studio().delete_kit(kit_id)
    return Response(status_code=204)


def asset_file(document):
    root = (data_root() / 'studio' / 'brand-assets').resolve()
    path = Path(document['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise not_found('Image is unavailable.')
    return str(path)


def inspect_image(path):
    """Pillow check that the bytes really are PNG or JPEG. Returns the media type and extension."""
    from PIL import Image
    try:
        with Image.open(path) as image:
            image.verify()
            kind = image.format
    except Exception:
        raise studio_media.UploadError(422, 'That file is not a valid PNG or JPEG image.') from None
    if kind not in {'PNG', 'JPEG'}:
        raise studio_media.UploadError(422, 'Upload a PNG or JPEG image.')
    return ('image/png', 'png') if kind == 'PNG' else ('image/jpeg', 'jpg')


@router.post('/brand-assets', status_code=201)
async def upload_brand_asset(request: Request):
    identity = uuid.uuid4().hex
    directory = data_root() / 'studio' / 'brand-assets' / identity
    try:
        _, file = await studio_media.receive_multipart(request, directory, MAX_ASSET_BYTES, asset_type, names=('file',))
        if file is None or not file['size']:
            raise studio_media.UploadError(422, 'Send the image in the "file" field.')
        media_type, extension = await to_thread.run_sync(inspect_image, file['path'])
        final = directory / f'logo.{extension}'
        file['path'].replace(final)
    except studio_media.UploadError as error:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(error.status, error.detail) from None
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    studio().add_asset({'id': identity, 'path': str(final), 'media_type': media_type, 'size': file['size']})
    return {'asset_id': identity, 'url': f'/api/studio/brand-assets/{identity}'}


@router.get('/brand-assets/{asset_id}')
def get_brand_asset(asset_id: str):
    if not valid_id(asset_id):
        raise not_found('Image not found.')
    try:
        document = studio().get_asset(asset_id)
    except KeyError:
        raise not_found('Image not found.') from None
    return FileResponse(asset_file(document), media_type=document['media_type'],
                        headers={'Cache-Control': 'private, max-age=3600'})


# -- projects ---------------------------------------------------------------

class ProjectOptions(Strict):
    aspect: Literal['9:16', '16:9'] = '9:16'
    count: Literal['auto'] | StrictInt = 'auto'
    remove_fillers: StrictBool = True
    max_duration: float = Field(default=60, gt=0, le=180, allow_inf_nan=False)
    template_id: StrictStr | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,64}$')
    brand_kit_id: StrictStr | None = Field(default=None, max_length=100)
    focus: StrictStr | None = Field(default=None, max_length=8000)

    @model_validator(mode='after')
    def counted(self):
        if not isinstance(self.count, str) and not 1 <= self.count <= MAX_COUNT:
            raise ValueError(f'count must be "auto" or from 1 to {MAX_COUNT}.')
        return self


class ProjectRequest(Strict):
    source_ids: list[StrictStr] = Field(min_length=1, max_length=12)
    name: StrictStr = Field(default='', max_length=100)
    options: ProjectOptions = Field(default_factory=ProjectOptions)
    request_id: StrictStr | None = Field(default=None, min_length=1, max_length=100)


def project_name(collection):
    if collection.get('name'):
        return collection['name']
    try:
        title = context().STORE.get(collection['source_ids'][0]).get('title')
    except KeyError:
        title = None
    return title if title and len(collection['source_ids']) == 1 else 'Untitled project'


def current_project_tasks(collection, tasks):
    return [task for task in tasks if not task.get('payload', {}).get('run_id') or
            task['payload']['run_id'] == collection.get('current_run')]


def project_state(collection, tasks, shorts):
    tasks = current_project_tasks(collection, tasks)
    run = collections().get_run(collection['current_run']) if collection.get('current_run') else None
    if collection['status'] == 'cancelled':
        return 'cancelled', run
    # Busy means work is actually queued or running. A run whose remaining tasks are held or failed is paused,
    # not processing, so the studio shows why and offers Resume instead of a frozen progress bar.
    busy = any(task['status'] in {'queued', 'active'} for task in tasks)
    stopped = any(task['status'] in {'failed', 'held_budget', 'held_dependency'} for task in tasks)
    # Between two stages no task is queued for a moment; an active run with nothing stopped is still working.
    if busy or (run and run.get('status') == 'active' and not stopped):
        return 'processing', run
    if any(short.get('current_revision') for short in shorts):
        return ('partially_ready' if stopped or any(short.get('held_revision') for short in shorts) else 'ready'), run
    if any(short.get('held_revision') for short in shorts):
        return 'needs_attention', run
    if not run and studio().project_options(collection['id']).get('start_error'):
        return 'failed', run
    return ('failed' if run else 'draft'), run


def project_progress(collection, tasks, run, status):
    if status == 'ready':
        return {'stage': 'Ready', 'fraction': 1.0}
    own = [t for t in tasks if run and t['payload'].get('run_id') == run['id']] if run else tasks
    done = sum(task['status'] == 'completed' for task in own)
    active = next((task for task in own if task['status'] == 'active'), None)
    stage = STAGE_LABELS.get(active['stage'], '') if active else ''
    return {'stage': stage, 'fraction': round(done / len(own), 3) if own else 0.0,
            **({'basis': 'recorded_tasks', 'completed_tasks': done, 'recorded_tasks': len(own),
                'label': f'{done} step{"s" if done != 1 else ""} finished'} if own else {})}


RESUMABLE_ERRORS = {'provider_quota', 'provider_network', 'provider_timeout', 'provider_service',
                    'provider_deadline', 'provider_retry_budget', 'budget_exhausted', 'dependency_failed',
                    'handler_failed', 'provider_config', 'provider_auth',  # fixable by an update, a key or a routing change
                    'editor_invalid_output'}  # model answers vary: asking again usually validates


def resumable(tasks):
    return any(t['status'] in {'held_budget', 'held_dependency'} or
               (t['status'] == 'failed' and t.get('error') in RESUMABLE_ERRORS) for t in tasks)


def project_error(tasks, run, start_error=None, collection=None):
    if start_error and not run:
        return f'Could not start: {start_error}'
    budget_task = next((t for t in reversed(tasks) if t['status'] == 'held_budget'), None)
    if budget_task:
        dimensions = (budget_task.get('budget_block') or {}).get('dimensions') or []
        if not dimensions and collection:
            # Legacy records have no structured block. A failed fallback needs
            # another envelope, even when the attempt's first call was admitted.
            dimensions = [key for key in ('tokens', 'calls') if budget_task.get('reservation', {}).get(key, 0)
                          > collection['budget'][key] - collection['usage'][key]]
        limit = ('token and request allowances' if set(dimensions) == {'tokens', 'calls'} else
                 'token allowance' if 'tokens' in dimensions else
                 'request allowance' if 'calls' in dimensions else 'generation allowance')
        return f'Paused: this step needs more {limit}. Resume to continue; finished steps are kept.'
    failed = next((task for task in reversed(tasks) if task['status'] == 'failed'), None)
    if failed and failed.get('error') == 'provider_quota':
        return ('Paused: the model provider rate-limited some steps. Resume to retry them; finished steps are kept. '
                'Resume after the provider cooldown, or configure a fallback in Settings.')
    if failed:
        text = ERROR_TEXT.get(failed.get('error'), 'Generation failed. Try again, or check your model key in Settings.')
        if failed.get('error') == 'handler_failed' and failed.get('error_detail'):
            text = f"A step failed inside Clippie ({failed['error_detail']}). Resume retries it once."
        return text
    if run and any(s.get('held_revision') for s in collections().list_shorts(run['collection_id'])):
        return 'Clips need editorial review before they can be used. Expand the editorial review on a clip below.'
    return 'No clips were produced. Try a different video or focus.' if run else None


def project_summary(collection, snap=None):
    from . import lifecycle, lifecycle_api
    tasks = current_project_tasks(collection, collections().list_tasks(collection['id']))
    shorts = collections().list_shorts(collection['id'])
    status, run = project_state(collection, tasks, shorts)
    ready = [short for short in shorts if short.get('current_revision')]
    first_source = collection['source_ids'][0] if collection['source_ids'] else None
    thumbnail = f'/api/studio/clips/{ready[0]["id"]}/thumbnail' if ready else (
        f'/api/studio/sources/{first_source}/thumbnail' if first_source else None)
    return {'id': collection['id'], 'name': project_name(collection), 'source_ids': collection['source_ids'],
            'status': status, 'clip_count': len(ready), 'created_at': tl.iso(collection['created_at']),
            'thumbnail_url': thumbnail, 'progress': project_progress(collection, tasks, run, status),
            **lifecycle.summary_fields(snap.ctx if snap else lifecycle_api.make_ctx(), collection, snap)}


def project_detail(collection):
    tasks = current_project_tasks(collection, collections().list_tasks(collection['id']))
    shorts = collections().list_shorts(collection['id'])
    status, run = project_state(collection, tasks, shorts)
    result = project_summary(collection)
    result.update(progress=project_progress(collection, tasks, run, status), options=studio().project_options(collection['id']) or None,
                  sources=[source_summary(job) for job in project_jobs(collection).values()],
                  clips=sorted_clips([clip_summary(short, tasks, collection) for short in shorts]))
    result['options'] = {k: v for k, v in (result['options'] or {}).items() if k not in ('id', 'start_error')}
    result['counts'] = {'ready': sum(bool(s.get('current_revision')) for s in shorts),
                        'held': sum(bool(s.get('held_revision')) for s in shorts),
                        'running': sum(t['status'] in {'queued', 'active'} for t in tasks),
                        'stopped': sum(t['status'] in {'failed', 'held_budget', 'held_dependency'} for t in tasks)}
    result['clip_counts'] = {key: sum(c['status'] in states for c in result['clips']) for key, states in {
        'ready': {'ready'}, 'held': {'needs_attention'}, 'processing': {'processing'},
        'waiting': {'waiting'}, 'stopped': {'paused', 'failed'}, 'cancelled': {'cancelled'}}.items()}
    result['task_counts'] = {'completed': sum(t['status'] == 'completed' for t in tasks),
                             'running': result['counts']['running'], 'stopped': result['counts']['stopped'],
                             'cancelled': sum(t['status'] == 'cancelled' for t in tasks)}
    if status in {'failed', 'partially_ready', 'needs_attention'}:
        result['error'] = project_error(tasks, run, (studio().project_options(collection['id']) or {}).get('start_error'), collection)
        result['resumable'] = resumable(tasks) or not run
    return result


@router.post('/projects/{project_id}/resume', status_code=202)
def resume_project(project_id: str):
    """Retry rate-limited or timed-out steps and extend the allowance for held work. Finished steps are kept."""
    from .collection_generation import provider
    from .collection_workflow import dispatch
    from .models.gateway import keys_message
    collection = get_collection(project_id)
    tasks = current_project_tasks(collection, collections().list_tasks(project_id))
    paid_pending = any(t['status'] in {'failed', 'held_budget', 'held_dependency'} and
                       any(t['reservation'].values()) for t in tasks)
    if (not collection.get('current_run') or paid_pending) and (message := keys_message(app_keys())):
        raise HTTPException(422, message)
    if not collection.get('current_run'):
        # Never started (an earlier start error, or a draft): start it now.
        from .collection_generation import start_generation
        options = studio().project_options(project_id) or {}
        try:
            start_generation(collections(), context().STORE, project_id, context().WORKER, request_id='resume-start')
        except ValueError as error:
            studio().set_project_options(project_id, {**options, 'start_error': str(error)[:300]})
            raise HTTPException(409, f'Could not start this project: {error}') from None
        studio().set_project_options(project_id, {k: v for k, v in options.items() if k != 'start_error'})
        return project_detail(collections().get_collection(project_id))
    tasks = current_project_tasks(collection, collections().list_tasks(project_id))
    if not resumable(tasks):
        raise HTTPException(409, 'Nothing to resume in this project.')
    stalled = [t for t in tasks if t['status'] in {'held_budget', 'held_dependency'}
               or (t['status'] == 'failed' and t.get('error') in RESUMABLE_ERRORS)]
    if collection.get('current_run') and paid_pending:
        # Renew the deadline and allowance first, so retried steps are not failed by the original run clock.
        try:
            collections().extend_run(collection['current_run'], tokens=(len(stalled) + 6) * provider.VOLUME_RESERVATION_TOKENS,
                                     calls=len(stalled) + 6, seconds=1800)
        except ValueError:
            pass
    for task in stalled:
        try:
            collections().retry_task(task['id'], grant=1)
        except ValueError:
            pass
    dispatch(collections(), context().STORE, project_id, context().WORKER)
    return project_detail(collections().get_collection(project_id))


# -- activity ---------------------------------------------------------------

# Failed exports and reframes leave the tray after a day (they have no dismiss action); publishing failures
# are listed separately and stay until resolved, since they may need a platform check.
ACTIVITY_FAILED_SECONDS = 24 * 3600
ACTIVITY_DONE_SECONDS = 600
JOB_LABELS = {'export': 'Exporting', 'auto_reframe': 'Reframing'}


def source_activity(job):
    summary = source_summary(job)
    state = 'running' if job['status'] in intake.BUSY_STATUSES else 'attention'
    if state == 'running' and not any(stage['status'] == 'active' for stage in summary['stages']):
        state = 'waiting'
    actions = ([{'id': 'cancel', 'label': 'Cancel'}] if summary['cancellable']
               else [{'id': 'retry', 'label': 'Retry'}] if summary['retryable'] else [])
    return {'kind': 'source', 'id': job['id'], 'title': summary['title'], 'state': state, 'status': job['status'],
            'fraction': summary['progress'], 'message': summary['message'] or summary['error'],
            'eta_seconds': summary['eta_seconds'], 'stalled': summary['stalled'], 'stall_reason': summary['stall_reason'],
            'thumbnail_url': summary['thumbnail_url'], 'stages': summary['stages'], 'queue': summary['queue'],
            'actions': actions, 'open': {'route': 'library'}}


def project_activity(collection):
    tasks = current_project_tasks(collection, collections().list_tasks(collection['id']))
    shorts = collections().list_shorts(collection['id'])
    status, run = project_state(collection, tasks, shorts)
    paused = status in {'failed', 'partially_ready', 'needs_attention'} and resumable(tasks)
    if status not in {'processing', 'partially_ready', 'needs_attention'} and not paused:
        return None
    progress = project_progress(collection, tasks, run, status)
    return {'kind': 'project', 'id': collection['id'], 'title': project_name(collection),
            'state': 'running' if status == 'processing' else 'attention', 'status': status,
            'fraction': None if progress.get('basis') == 'recorded_tasks' else progress['fraction'],
            'message': ((progress['stage'] or 'Working') + (f" · {progress['label']}" if progress.get('label') else '')
                        if status == 'processing' else project_error(tasks, run, collection=collection)),
            'eta_seconds': None, 'stalled': False, 'stall_reason': None, 'thumbnail_url': None, 'stages': [], 'queue': None,
            'actions': [{'id': 'resume', 'label': 'Resume'}] if paused else [], 'open': {'route': 'project', 'id': collection['id']}}


def studio_job_activity(job, moment):
    age = moment - job.get('updated_at', moment)
    if job['kind'] not in JOB_LABELS or (job['status'] == 'failed' and age > ACTIVITY_FAILED_SECONDS) \
            or (job['status'] == 'done' and age > ACTIVITY_DONE_SECONDS):
        return None
    title, destination = JOB_LABELS[job['kind']], {'route': 'library'}
    try:
        short, collection = clip_context(job['clip_id'])
        title = f'{JOB_LABELS[job["kind"]]} {project_name(collection)}'
        destination = {'route': 'clip', 'id': short['id']}
    except HTTPException:
        pass
    state = {'queued': 'waiting', 'running': 'running', 'failed': 'attention', 'done': 'done'}[job['status']]
    result = job.get('result') or {}
    return {'kind': 'reframe' if job['kind'] == 'auto_reframe' else job['kind'], 'id': job['id'], 'title': title, 'state': state, 'status': job['status'],
            'fraction': job.get('progress') or 0.0,
            'message': job.get('error') if job['status'] == 'failed' else 'Ready to download' if job['status'] == 'done' else job.get('message') or ('Waiting to start' if job['status'] == 'queued' else JOB_LABELS[job['kind']] + '…'),
            'eta_seconds': None, 'stalled': False, 'stall_reason': None, 'thumbnail_url': None, 'stages': [], 'queue': None,
            'diagnostics': {'attempt': job.get('attempt', 1), 'updated_at': job.get('updated_at'),
                            'events': studio().job_events(job['id'])},
            'actions': [], 'open': {'url': result['video_url']} if result.get('video_url') else destination}


@router.get('/activity')
def activity():
    """Everything running in the background or needing attention: source intake, project generation, exports."""
    import time
    items = [source_activity(job) for job in context().STORE.all()
             if job['status'] in intake.BUSY_STATUSES or job['status'] in {'failed', 'cancelled'} and not job.get('words')][:30]
    items += [item for collection in collections().list_collections()
              if not collection.get('trashed_at') and (item := project_activity(collection))]
    moment = time.time()
    items += [item for job in studio().list_jobs()[:40] if (item := studio_job_activity(job, moment))]
    if (data_root() / 'publish.sqlite3').exists():
        from .publish import runtime
        publishing = runtime.store(data_root())
        for post in publishing.posts():
            if post['status'] not in ('scheduled', 'uploading', 'failed'):
                continue
            status = post['status']
            message = post.get('error') or ('Uploading to the platform' if status == 'uploading' else
                      'Waiting for the export' if post.get('render_job_id') else 'Waiting to upload')
            if status == 'failed' and post.get('render_job_id'):
                try:
                    linked = studio().get_job(post['render_job_id'])
                    if linked.get('status') == 'done' and linked.get('clip_id') == post['short_id']:
                        message = 'Export finished after the interruption. Open Publishing to continue.'
                except KeyError:
                    pass
            if status == 'scheduled' and post.get('next_attempt_at') and post['next_attempt_at'] > moment:
                message = 'Retrying after ' + time.strftime('%H:%M', time.localtime(post['next_attempt_at']))
            items.append({'kind': 'publish', 'id': post['id'], 'title': post['title'],
                          'state': 'attention' if status == 'failed' else 'running' if status == 'uploading' else 'waiting',
                          'status': status, 'fraction': None, 'message': message, 'eta_seconds': None,
                          'stalled': False, 'stall_reason': None, 'thumbnail_url': None, 'stages': [], 'queue': None,
                          'diagnostics': {'attempt': post.get('attempts', 0), 'updated_at': post.get('updated_at'),
                                          'events': publishing.post_events(post['id'])},
                          'actions': [], 'open': {'route': 'publishing', 'id': post['short_id']}})
    running = sum(item['state'] in {'running', 'waiting'} for item in items)
    return {'items': items, 'running': running, 'attention': sum(item['state'] == 'attention' for item in items)}


def app_keys():
    from . import app
    return app.KEYS


def get_collection(project_id):
    if not valid_id(project_id):
        raise not_found('Project not found.')
    try:
        return collections().get_collection(project_id)
    except KeyError:
        raise not_found('Project not found.') from None


def check_project_options(options):
    if options.brand_kit_id:
        get_kit_or_422(options.brand_kit_id)
    if options.template_id:
        try:
            if options.template_id not in engine.load_templates()['captions']:
                raise HTTPException(422, f'Unknown caption template "{options.template_id}".')
        except engine.EngineUnavailable:
            pass


def get_kit_or_422(kit_id):
    try:
        return get_kit(kit_id)
    except HTTPException:
        raise HTTPException(422, 'Unknown brand kit.') from None


@router.post('/projects', status_code=201)
def create_project(body: ProjectRequest):
    from .collection_api import CollectionRequest, Constraints, create_collection
    from .collection_generation import start_generation
    from .app import sources_for
    from pydantic import ValidationError
    app, options = context(), body.options
    for source_id in body.source_ids:
        try:
            app.STORE.get(source_id)
        except KeyError:
            raise HTTPException(422, 'Choose sources from your library.') from None
    jobs = sources_for(body.source_ids, require_speech=True)
    from .models.gateway import keys_message
    if (message := keys_message(app.KEYS)):
        raise HTTPException(422, message)
    check_project_options(options)
    roles = {sid: 'primary' if jobs[sid].get('words') else 'picture_only' for sid in body.source_ids}
    try:
        constraints = Constraints(aspect_ratio=options.aspect, max_shorts=AUTO_COUNT if options.count == 'auto' else options.count,
                                  max_duration=options.max_duration, focus=options.focus or '', source_roles=roles)
        request = CollectionRequest(source_ids=body.source_ids, name=body.name, request_id=body.request_id, constraints=constraints)
    except ValidationError as error:
        raise HTTPException(422, '; '.join(item['msg'] for item in error.errors())) from None
    created = create_collection(request)
    studio().set_project_options(created['id'], {**options.model_dump(), 'count': options.count})
    try:
        start_generation(collections(), app.STORE, created['id'], app.WORKER, request_id=body.request_id or 'initial')
    except ValueError as error:
        # Keep the project visible with the reason instead of a silent 0% draft.
        studio().set_project_options(created['id'], {**studio().project_options(created['id']),
                                                     'start_error': str(error)[:300]})
        raise HTTPException(409, f'Could not start this project: {error}') from None
    return project_detail(collections().get_collection(created['id']))


@router.get('/projects')
def list_projects(include: str = ''):
    from . import lifecycle, lifecycle_api
    snap = lifecycle.Snap(lifecycle_api.make_ctx())
    return [project_summary(item, snap) for item in reversed(snap.collections)
            if include == 'trashed' or not snap.is_trashed(item)]


@router.get('/projects/{project_id}')
def get_project(project_id: str):
    return project_detail(get_collection(project_id))


# -- static studio app and fonts --------------------------------------------

MISSING_BUILD = ('Clippie Studio has not been built yet.\n'
                 'Build the web app (see web/README) so clippie/static/studio/index.html exists, then reload.\n')


def inside(root, relative):
    candidate = (root / relative).resolve()
    return candidate if candidate.is_relative_to(root.resolve()) and candidate.is_file() else None


def index_page():
    page = inside(STATIC, 'index.html')
    if page is None:
        return PlainTextResponse(MISSING_BUILD, status_code=404)
    return FileResponse(page, media_type='text/html', headers={'Cache-Control': 'no-store'})


@static_router.get('/studio')
def studio_index():
    return index_page()


@static_router.get('/studio/{path:path}')
def studio_path(path: str):
    if path in {'third-party-notices.txt', 'third-party-notices.provenance.json'}:
        found = inside(STATIC, path)
        if found is None:
            raise not_found('Notice file not found.')
        return FileResponse(found, headers={'Cache-Control': 'no-cache'})
    if path.startswith('assets/'):
        found = inside(STATIC / 'assets', path[len('assets/'):])
        if found is None:
            raise not_found('Asset not found.')
        return FileResponse(found, headers={'Cache-Control': 'public, max-age=31536000, immutable'})
    return index_page()


@static_router.get('/static/fonts/{file}')
def font_file(file: str):
    if not FONT_FILE.fullmatch(file):
        raise not_found('Font not found.')
    found = inside(FONTS_DIR, file)
    if found is None:
        raise not_found('Font not found.')
    return FileResponse(found, headers={'Cache-Control': 'public, max-age=31536000, immutable'})
