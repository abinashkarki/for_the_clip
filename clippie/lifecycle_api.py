"""HTTP surface for the video lifecycle (docs/VIDEO_LIFECYCLE_PLAN_2026-10-28.md). Routes live under /api/studio."""
import functools
import re
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from . import lifecycle
from .lifecycle import LifecycleError

router = APIRouter(prefix='/api/studio', tags=['lifecycle'])

# Mutating routes that must refuse a freed or trashed project: (method, pattern, kind of id captured)
GUARDED = [
    (re.compile(r'^/api/studio/clips/([^/]+)/(?:timeline|export|actions/[^/]+)$'), 'clip'),
    (re.compile(r'^/api/studio/projects/([^/]+)/resume$'), 'project'),
    (re.compile(r'^/api/collections/([^/]+)/(?:prepare|generate|continue|exports)$'), 'project'),
    (re.compile(r'^/api/collections/([^/]+)/shorts/[^/]+/(?:more|improve|settings|restore)$'), 'project'),
]
MUTATING = {'POST', 'PUT', 'PATCH', 'DELETE'}


def make_ctx():
    from . import studio_api
    from .publish import runtime
    app = studio_api.context()
    root = app.STORE.root

    def publish():
        return runtime.store(root) if (root / 'publish.sqlite3').exists() else None
    return lifecycle.Ctx(root, studio_api.collections(), app.STORE, studio_api.studio(), publish=publish,
                         retry=app.retry_job, project_name=studio_api.project_name)


def failing(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LifecycleError as error:
            raise HTTPException(error.status, error.message) from None
    return call


def summary(ctx, collection):
    from . import studio_api
    return studio_api.project_summary(ctx.collections.get_collection(collection['id']))


def project_for_path(ctx, kind, identity):
    try:
        if kind == 'clip':
            return ctx.collections.get_collection(ctx.collections.get_short(identity)['collection_id'])
        return ctx.collections.get_collection(identity)
    except KeyError:
        return None


def guard(method, path):
    """(status, message) when this request must be refused, else None. Used by the app middleware."""
    if method not in MUTATING:
        return None
    for pattern, kind in GUARDED:
        match = pattern.match(path)
        if match:
            ctx = make_ctx()
            collection = project_for_path(ctx, kind, match.group(1))
            reason = lifecycle.guard_reason(ctx, collection) if collection else None
            return (409, reason) if reason else None
    return None


def touched(method, path):
    """Project id to mark active after a successful mutating request, else None."""
    if method not in MUTATING:
        return None
    for pattern, kind in GUARDED:
        match = pattern.match(path)
        if match:
            collection = project_for_path(make_ctx(), kind, match.group(1))
            return collection['id'] if collection else None
    return None


def touch_project(project_id):
    lifecycle.touch(make_ctx(), project_id)


@router.post('/projects/{project_id}/trash')
@failing
def trash_project(project_id: str):
    ctx = make_ctx()
    return summary(ctx, lifecycle.trash(ctx, project_id))


@router.post('/projects/{project_id}/untrash')
@failing
def untrash_project(project_id: str):
    ctx = make_ctx()
    return summary(ctx, lifecycle.untrash(ctx, project_id))


@router.delete('/projects/{project_id}', status_code=204)
@failing
def purge_project(project_id: str):
    lifecycle.purge_project(make_ctx(), project_id)
    return Response(status_code=204)


@router.post('/projects/{project_id}/free-source')
@failing
def free_source(project_id: str):
    ctx = make_ctx()
    return summary(ctx, lifecycle.free_source(ctx, project_id))


class KeepBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    keep: StrictBool


@router.post('/projects/{project_id}/keep-source')
@failing
def keep_source(project_id: str, body: KeepBody):
    ctx = make_ctx()
    return summary(ctx, lifecycle.keep_source(ctx, project_id, body.keep))


@router.post('/projects/{project_id}/restore-source', status_code=202)
@failing
def restore_source(project_id: str):
    ctx = make_ctx()
    return summary(ctx, lifecycle.restore_source(ctx, project_id))


@router.post('/projects/{project_id}/sources/{source_id}/restore-upload')
async def restore_upload(project_id: str, source_id: str, request: Request):
    """Stream a matching original into its existing source, under the worker lock."""
    from . import job_locks, studio_api, media
    ctx = make_ctx()
    try:
        collection = lifecycle.require(ctx, project_id)
    except LifecycleError as error:
        raise HTTPException(error.status, error.message) from None
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source_id) or source_id not in collection['source_ids']:
        raise HTTPException(404, 'Source not found in this project.')
    if not job_locks.acquire(ctx.root, source_id):
        raise HTTPException(409, 'This source is busy. Try again in a moment.')
    staged = None
    stage_dir = ctx.root / 'restore-staging' / source_id
    try:
        snap = lifecycle.Snap(ctx)
        if snap.is_trashed(collection):
            raise LifecycleError(409, lifecycle.TRASHED_MESSAGE)
        try:
            job = ctx.jobs.get(source_id)
        except KeyError:
            raise LifecycleError(404, 'Source not found.') from None
        if lifecycle.is_link_source(job) or not lifecycle.source_is_freed(job):
            raise LifecycleError(409, 'This source does not need an uploaded original.')
        for user in snap.users.get(source_id, []):
            if reason := snap.blocked(user):
                raise LifecycleError(409, reason)
        expected, size = job.get('source_sha256'), job.get('source_size')
        if not expected or not isinstance(size, int) or size <= 0:
            raise LifecycleError(422, 'This older source has no original fingerprint, so it cannot be safely reattached.')
        if size > studio_api.upload_limit():
            raise LifecycleError(413, 'The original exceeds the upload limit.')
        target = Path(str(job.get('source_path') or ''))
        uploads = (ctx.root / 'uploads').resolve()
        if not target.is_absolute() or target.parent.parent.resolve() != uploads or target.parent.is_symlink() or target.is_symlink() or target.name in ('', '.', '..'):
            raise LifecycleError(422, 'This source does not have a safe managed upload location.')
        uploads.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(uploads).free < size:
            raise LifecycleError(507, 'Not enough free space to restore this original.')
        stage_dir.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix='upload-', dir=stage_dir)
        digest, received = hashlib.sha256(), 0
        with os.fdopen(fd, 'wb') as output:
            async for chunk in request.stream():
                received += len(chunk)
                if received > size:
                    raise LifecycleError(422, 'This file does not match the original.')
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if received != size or digest.hexdigest() != expected:
            raise LifecycleError(422, 'This file does not match the original.')
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        staged = None
        try:
            preview = await to_thread.run_sync(lambda: media.make_preview(target, ctx.root / source_id, duration=job.get('duration')))
        except (RuntimeError, OSError) as error:
            raise LifecycleError(422, f'The original matched, but its preview could not be prepared: {error}') from None
        ctx.jobs.update(source_id, source_freed_at=None, preview_path=str(preview), status='ready', error=None)
        lifecycle.touch(ctx, project_id)
        return summary(ctx, collection)
    except LifecycleError as error:
        raise HTTPException(error.status, error.message) from None
    except OSError as error:
        if error.errno == 28:
            raise HTTPException(507, 'Not enough free space to restore this original.') from None
        raise
    finally:
        if staged:
            Path(staged).unlink(missing_ok=True)
        try:
            stage_dir.rmdir()
        except OSError:
            pass
        job_locks.release(ctx.root, source_id)


@router.post('/sources/{source_id}/dismiss', status_code=204)
@failing
def dismiss_source(source_id: str):
    lifecycle.dismiss_source(make_ctx(), source_id)
    return Response(status_code=204)


@router.get('/storage')
@failing
def get_storage():
    return lifecycle.storage(make_ctx())


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    retention_days: StrictInt


@router.post('/storage/settings')
@failing
def storage_settings(body: StorageSettings):
    ctx = make_ctx()
    ctx.set_retention_days(body.retention_days)
    return lifecycle.storage(ctx)


@router.post('/storage/free')
@failing
def storage_free():
    if not lifecycle.DESTRUCTIVE_LIFECYCLE_ENABLED:
        raise LifecycleError(503, lifecycle.DELETION_PAUSED)
    ctx = make_ctx()
    lifecycle.sweep(ctx)
    return lifecycle.storage(ctx)
