"""Opt-in collection foundation. Creating a workspace does not run an editor."""
import json
import unicodedata
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool, StrictStr, model_validator

from .collection_store import CollectionStore, CollectionRequestConflict

router = APIRouter(prefix='/api/collections', tags=['collections'])
_STORES = {}


def context():
    from . import app
    return app


def store():
    root = context().STORE.root
    if str(root) not in _STORES:
        _STORES[str(root)] = CollectionStore(root)
    return _STORES[str(root)]


class Constraints(BaseModel):
    model_config = ConfigDict(extra='forbid')
    aspect_ratio: Literal['9:16', '16:9'] = '9:16'
    max_shorts: StrictInt = Field(default=12, ge=1, le=30)
    form: Literal['auto', 'moment', 'story'] = 'auto'
    edit_format: Literal['auto', 'explanation', 'small_story', 'exchange'] = 'auto'
    captions: bool = True
    caption_style: Literal['auto', 'clean_focus', 'editorial', 'phrase'] = 'auto'
    canvas_style: Literal['plain', 'blurred'] = 'plain'
    focus: str = Field(default='', max_length=8000)
    audience: str = Field(default='', max_length=1000)
    exclusions: list[str] = Field(default_factory=list, max_length=40)
    max_duration: float = Field(default=180, ge=1, le=180, allow_inf_nan=False)
    preferred_duration: float | None = Field(default=None, ge=1, le=180, allow_inf_nan=False)
    source_roles: dict[str, Literal['primary', 'picture_only']] = Field(default_factory=dict)
    locked_unit_ids: list[str] = Field(default_factory=list, max_length=96)
    # Allow a short verbatim flash-forward from later in a clip to play first when the natural opening is weak.
    cold_open: bool = True

    @model_validator(mode='after')
    def durations(self):
        if self.preferred_duration is not None and self.preferred_duration > self.max_duration:
            raise ValueError('Preferred duration must fit the hard maximum.')
        if any(not value.strip() or len(value) > 1000 for value in self.exclusions):
            raise ValueError('Exclusions must be non-empty text of at most 1000 characters.')
        return self


class Budget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # Zero explicitly prevents paid work until execution presets are benchmarked.
    tokens: StrictInt = Field(default=0, ge=0, le=1_000_000)
    calls: StrictInt = Field(default=0, ge=0, le=100)


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_ids: list[str] = Field(min_length=1, max_length=12)
    name: StrictStr = Field(default='', max_length=100)
    request_id: StrictStr | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def metadata(self):
        if any(unicodedata.category(c) == 'Cc' for c in self.name):
            raise ValueError('Name cannot contain control characters.')
        self.name = self.name.strip()
        if self.request_id is not None and not self.request_id.strip():
            raise ValueError('Request ID must be nonempty.')
        return self

    constraints: Constraints = Field(default_factory=Constraints)
    budget: Budget = Field(default_factory=Budget)


def public(document, *, detail=False):
    result = dict(document)
    try:
        source_title = context().STORE.get(document['source_ids'][0]).get('title')
    except KeyError:
        source_title = None
    result['title'] = document.get('name') or (source_title if len(document['source_ids']) == 1 and source_title else 'Source collection')
    result['capabilities'] = {'generation': True, 'prepare_evidence': True,
                              'gemini_configured': not _keys_message(context().KEYS)}
    if document.get('current_run'):
        result['run'] = store().get_run(document['current_run'])
        result['run'].pop('configuration', None)
    if detail:
        result['usage_report'] = store().usage_report(document['id'])
        result['shorts'] = [public_short(item) for item in store().list_shorts(document['id'])]
        # Do not expose task payloads/results, which can contain private evidence.
        safe_fields = {'id', 'stage', 'status', 'short_id', 'attempts', 'max_attempts', 'error', 'created_at', 'updated_at'}
        tasks = store().list_tasks(document['id'])
        if document.get('current_run'):
            tasks = [task for task in tasks if task['payload'].get('run_id') == document['current_run']
                     or (task['stage'] == 'prepare_source' and task['status'] in {'queued', 'active'})]
        result['tasks'] = [{key: value for key, value in task.items() if key in safe_fields} for task in tasks]
        result['visual_evidence'] = visual_evidence(document['id'], tasks)
        result['additional_discovery'] = []
        for task in tasks:
            if task['stage'] != 'discover_more':
                continue
            children = [t for t in tasks if t['payload'].get('more_task') == task['id']]
            shorts = [s for s in store().list_shorts(document['id']) if s.get('metadata', {}).get('more_task') == task['id']]
            status = task['status']
            if children:
                pending = next((t for t in reversed(children) if t['status'] != 'completed'), None)
                status = pending['status'] if pending else 'completed'
            operation = {'task_id': task['id'], 'status': status,
                         'explanation': (task.get('result') or {}).get('explanation', '')}
            if shorts:
                operation['short_id'] = shorts[0]['id']
            result['additional_discovery'].append(operation)
        active = next((task for task in tasks if task['status'] == 'active'), None)
        result['progress'] = {'map_source': 'Understanding video', 'discover': 'Choosing distinct ideas', 'discover_more': 'Finding one more distinct idea',
            'plan_short': 'Editing a short', 'review_short': 'Checking the edit', 'review_batch': 'Checking titles and hooks',
            'repair_batch': 'Fixing titles and hooks', 'compile_short': 'Building the edit',
            'render_preview': 'Making a preview', 'render_export': 'Exporting video', 'apply_settings': 'Applying visual settings'}.get(active['stage'], '') if active else ''
        result['summary'] = {name: sum(short['status'] == value for short in result['shorts'])
                             for name, value in [('ready', 'ready'), ('needs_attention', 'needs_attention'),
                                                 ('failed', 'failed'), ('active', 'active')]}
        if result.get('run'):
            discoveries = [task for task in tasks
                           if task['stage'] == 'discover' and task['status'] == 'completed']
            if discoveries:
                result['run']['explanation'] = (discoveries[-1].get('result') or {}).get('explanation', '')
    return result


def public_artifact(artifact):
    return {'id': artifact['id'], 'revision_id': artifact['revision_id'], 'kind': artifact['kind'],
            'profile': artifact['document']['profile'],
            **{kind + '_url': f'/api/collections/{artifact["collection_id"]}/artifacts/{artifact["id"]}/{kind}'
               for kind in ('video', 'subtitles', 'manifest')}}


def public_short(short):
    result = dict(short)
    revision_id = short['current_revision'] or short.get('pending_revision')
    result['revision'] = store().get_revision(revision_id) if revision_id else None
    held_id = short.get('held_revision')
    result['held'] = None
    if held_id:
        held_decisions = store().list_decisions(short['id'], held_id)
        result['held'] = {'revision': store().get_revision(held_id),
            'preview': public_artifact(store().get_artifact(short['held_preview'])) if short.get('held_preview') else None,
            'decision': held_decisions[-1] if held_decisions else None}
    for kind in ('preview', 'export'):
        artifact_id = short.get('current_' + kind)
        if not artifact_id and kind == 'export':
            exports = [artifact for artifact in store().list_artifacts(short['id'])
                       if artifact['kind'] == 'export' and artifact['revision_id'] == short['current_revision']]
            artifact_id = exports[-1]['id'] if exports else None
        result[kind] = public_artifact(store().get_artifact(artifact_id)) if artifact_id else None
    tasks = [task for task in store().list_tasks(short['collection_id']) if task['short_id'] == short['id']]
    pending_tasks = [task for task in tasks if task['id'] not in short.get('restored_task_ids', [])]
    reviews = [task for task in pending_tasks if task['stage'] == 'review_short' and task['status'] == 'completed']
    plans = [task for task in pending_tasks if task['stage'] == 'plan_short' and task['status'] == 'completed']
    latest_review = (reviews[-1].get('result') or {}) if reviews else {}
    result['pending_review'] = latest_review if latest_review.get('status') in {'needs_revision', 'uncertain'} else None
    result['pending_plan'] = (plans[-1].get('result') or {}) if plans else None
    busy = any(task['status'] in {'active', 'queued'} for task in tasks)
    failed = [task for task in tasks if task['status'] == 'failed']
    # Generated revisions are final without a server preview (the studio previews live and renders at export).
    compiled = bool(short['current_revision'] and result['revision']['document'].get('artifact_state') == 'timeline')
    result['status'] = ('active' if busy else 'needs_attention' if result['held'] else 'ready' if result['preview'] or compiled else 'failed' if failed else 'needs_attention')
    result['error'] = tasks[-1]['error'] if tasks and tasks[-1]['status'] == 'failed' and not busy else None
    decisions = store().list_decisions(short['id'], short['current_revision']) if short['current_revision'] else []
    result['decision'] = decisions[-1] if decisions else None
    review_state = store().export_review_state(revision_id) if revision_id else {}
    result['export_review_required'] = review_state.get('export_review_required', False)
    result['export_review_acknowledged'] = review_state.get('export_review_acknowledged', False)
    return result


def short_in(collection_id, short_id):
    collection(collection_id)
    try:
        short = store().get_short(short_id)
    except KeyError:
        raise HTTPException(404, 'Short not found.')
    if short['collection_id'] != collection_id:
        raise HTTPException(404, 'Short not found in this collection.')
    return short


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(default='initial', min_length=1, max_length=100)
    calls: StrictInt = Field(default=16, ge=1, le=24)
    seconds: StrictInt = Field(default=600, ge=30, le=900)


class ContinueRequest(GenerationRequest):
    request_id: str = Field(min_length=1, max_length=100)
    calls: StrictInt = Field(default=8, ge=1, le=24)


class ImproveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    edit_format: Literal['auto', 'explanation', 'small_story', 'exchange'] | None = None
    caption_style: Literal['auto', 'clean_focus', 'editorial', 'phrase'] | None = None
    base_revision: str | None = None
    instruction: str = Field(min_length=1, max_length=8000)
    locked_unit_ids: list[str] = Field(default_factory=list, max_length=96)


class Framing(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    mode: Literal['fit', 'fill']
    zoom: float = Field(ge=1, le=1.5, allow_inf_nan=False)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_revision: StrictStr = Field(min_length=1, max_length=100)
    request_id: StrictStr = Field(min_length=1, max_length=100)
    captions: StrictBool = True
    caption_style: Literal['clean_focus', 'editorial', 'phrase'] = 'clean_focus'
    canvas_style: Literal['plain', 'blurred'] = 'plain'
    framing: Framing = Field(default_factory=lambda: Framing(mode='fit', zoom=1, x=.5, y=.5))
    shot_framing: dict[str, Framing] = Field(default_factory=dict, max_length=96)

    @model_validator(mode='after')
    def bounded_keys(self):
        if any(not key.strip() or len(key) > 200 for key in self.shot_framing):
            raise ValueError('Invalid evidence unit ID.')
        if not self.base_revision.strip() or not self.request_id.strip():
            raise ValueError('Revision and request IDs must be nonempty.')
        return self


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision_id: str = Field(min_length=1, max_length=100)
    expected_revision: str | None
    request_id: str = Field(min_length=1, max_length=100)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision_id: str
    rating: Literal['keep', 'revise', 'reject']
    notes: str = Field(default='', max_length=8000)
    acknowledge_warnings: StrictBool = False


class ExportSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    short_id: str
    revision_id: str


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revisions: list[ExportSelection] = Field(min_length=1, max_length=12)


def collection(collection_id):
    try:
        return store().get_collection(collection_id)
    except KeyError:
        raise HTTPException(404, 'Collection not found.')


@router.get('')
def list_collections():
    return [public(item) for item in store().list_collections()]


def visual_evidence(collection_id, tasks=None):
    groups = []
    seen = set()
    for task in tasks if tasks is not None else store().list_tasks(collection_id):
        mapped = task.get('result') or {}
        evidence = mapped.get('visual_evidence') or {}
        if isinstance(evidence, list):
            for entry in evidence:
                key = entry['artifact_key']
                if key not in seen:
                    seen.add(key)
                    sample = {field: entry[field] for field in ('id', 'source_id', 'at')}
                    sample.update(sent_to_editor=True,
                        image_url=f"/api/collections/{collection_id}/visuals/{key}/{entry['id']}")
                    groups.append({'artifact_key': key, 'samples': [sample], 'omitted_count': 0,
                                   'observations': [], 'purpose': 'supporting_picture'})
            continue
        key = evidence.get('artifact_key')
        if not key or key in seen:
            continue
        seen.add(key)
        sent = set(evidence.get('sample_ids', []))
        samples = [{**sample, 'sent_to_editor': sample['id'] in sent,
                    'image_url': f"/api/collections/{collection_id}/visuals/{key}/{sample['id']}"}
                   for sample in evidence.get('samples', [])]
        groups.append({'artifact_key': key, 'samples': samples,
                       'omitted_count': evidence.get('omitted_count', 0),
                       'observations': mapped.get('visual_notes', [])})
    return groups


@router.get('/{collection_id}/visuals/{artifact_key}/{sample_id}')
def visual_sample(collection_id: str, artifact_key: str, sample_id: str):
    collection(collection_id)
    groups = visual_evidence(collection_id)
    if not any(group['artifact_key'] == artifact_key and any(s['id'] == sample_id for s in group['samples'])
               for group in groups):
        raise HTTPException(404, 'Sample not referenced by this collection.')
    from .source_visuals import load_samples
    try:
        prepared = load_samples(store().root / 'source-visuals', artifact_key)
        sample = next(s for s in prepared['artifact']['samples'] if s['id'] == sample_id)
    except (ValueError, StopIteration):
        raise HTTPException(409, 'Saved visual evidence is unavailable or damaged; other saved media remains accessible.') from None
    return FileResponse(sample['path'], media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=31536000, immutable'})


@router.post('', status_code=201)
def create_collection(body: CollectionRequest):
    from .app import sources_for
    roles = body.constraints.source_roles
    if set(roles) - set(body.source_ids):
        raise HTTPException(422, 'Source roles must reference selected sources.')
    roles = {source_id: roles.get(source_id, 'primary') for source_id in body.source_ids}
    constraints = body.constraints.model_dump()
    constraints['source_roles'] = roles
    inputs = dict(constraints=constraints, budget=body.budget.model_dump(),
                  name=body.name, request_id=body.request_id)
    try:
        document = store().find_collection_request(body.source_ids, **inputs)
        if document is None:
            sources = sources_for(body.source_ids, require_speech=False)
            if not any(roles[source_id] == 'primary' and source.get('words') for source_id, source in sources.items()):
                raise HTTPException(422, 'Include a primary source with original speech.')
            document = store().create_collection(body.source_ids, **inputs)
    except CollectionRequestConflict as error:
        raise HTTPException(409, str(error))
    except ValueError as error:
        raise HTTPException(422, str(error))
    return public(document, detail=True)


@router.get('/{collection_id}')
def get_collection(collection_id: str):
    return public(collection(collection_id), detail=True)


@router.post('/{collection_id}/prepare', status_code=202)
def prepare_collection(collection_id: str):
    from .collection_workflow import queue_preparation
    from .app import sources_for
    document = collection(collection_id)
    sources_for(document['source_ids'], require_speech=False)
    try:
        queued = queue_preparation(store(), context().STORE, collection_id, context().WORKER)
    except ValueError as error:
        raise HTTPException(409, str(error))
    return public(queued, detail=True)


@router.post('/{collection_id}/tasks/{task_id}/retry', status_code=202)
def retry_preparation(collection_id: str, task_id: str):
    from .collection_workflow import dispatch
    collection(collection_id)
    task = next((item for item in store().list_tasks(collection_id) if item['id'] == task_id), None)
    if task is None:
        raise HTTPException(404, 'Task not found in this collection.')
    if task['stage'] not in {'prepare_source', 'map_source', 'discover', 'discover_more', 'plan_short', 'review_short', 'review_batch', 'repair_batch', 'compile_short', 'render_preview', 'render_export', 'apply_settings'}:
        raise HTTPException(409, 'This stage is not connected yet.')
    try:
        store().retry_task(task_id)
    except ValueError as error:
        raise HTTPException(409, str(error))
    dispatch(store(), context().STORE, collection_id, context().WORKER)
    return public(collection(collection_id), detail=True)


@router.post('/{collection_id}/cancel')
def cancel_collection(collection_id: str):
    collection(collection_id)
    return public(store().cancel_collection(collection_id), detail=True)


@router.post('/{collection_id}/generate', status_code=202)
def generate_collection(collection_id: str, body: GenerationRequest):
    from .collection_generation import start_generation
    document = collection(collection_id)
    if (message := _keys_message(context().KEYS)):
        raise HTTPException(422, message)
    from .app import sources_for
    sources_for(document['source_ids'], require_speech=True)
    try:
        start_generation(store(), context().STORE, collection_id, context().WORKER, **body.model_dump())
    except ValueError as error:
        raise HTTPException(409, str(error))
    return public(collection(collection_id), detail=True)


@router.post('/{collection_id}/continue', status_code=202)
def continue_collection(collection_id: str, body: ContinueRequest):
    from .collection_generation import provider
    from .collection_workflow import dispatch
    document = collection(collection_id)
    if not document.get('current_run'):
        raise HTTPException(409, 'Start a collection run first.')
    if (message := _keys_message(context().KEYS)):
        raise HTTPException(422, message)
    try:
        store().continue_run(document['current_run'], body.calls * provider.RESERVATION_TOKENS,
                             body.calls, body.seconds, body.request_id)
    except ValueError as error:
        raise HTTPException(409, str(error))
    dispatch(store(), context().STORE, collection_id, context().WORKER)
    return public(collection(collection_id), detail=True)


@router.get('/{collection_id}/shorts/{short_id}')
def short_detail(collection_id: str, short_id: str):
    short = short_in(collection_id, short_id)
    result = public_short(short)
    transcript_revision = (result['revision'] or (result['held']['revision'] if result.get('held') else None)
                           or {'document': {'plan': result.get('pending_plan')}})
    result['transcript'] = revision_transcript(transcript_revision)
    result['history'] = store().list_revisions(short_id)
    for revision in result['history']:
        revision['compiled_speech'] = revision_transcript(revision)
    result['artifacts'] = [public_artifact(artifact) for artifact in store().list_artifacts(short_id)]
    return result


def revision_transcript(revision):
    """Resolve only the immutable plan's own evidence, preserving output order."""
    plan = revision.get('document', {}).get('plan') or {}
    from .word_spans import resolve_plan
    artifacts = []
    try:
        for fingerprint in plan.get('evidence_provenance', {}).get('sources', {}).values():
            path = store().root / 'source-evidence' / f'evidence-{fingerprint}.json'
            artifacts.append(json.loads(path.read_text()))
        return resolve_plan(plan, artifacts) if plan.get('unit_ids') else []
    except (OSError, ValueError, KeyError, TypeError):
        return [{'id': identity, 'unavailable': True, 'reason': 'Saved speech evidence or span proof is unavailable or invalid.'}
                for identity in plan.get('unit_ids', [])]



@router.post('/{collection_id}/shorts/{short_id}/improve', status_code=202)
def improve(collection_id: str, short_id: str, body: ImproveRequest):
    from .collection_generation import improve_short, key
    short = short_in(collection_id, short_id)
    if (message := _keys_message(context().KEYS)):
        raise HTTPException(422, message)
    request_inputs = body.model_dump()
    if short.get('restoration_id'):
        request_inputs['restoration_id'] = short['restoration_id']
    request_id = key(request_inputs)
    try:
        improve_short(store(), context().STORE, collection_id, short_id, context().WORKER,
                      request_id=request_id, **body.model_dump())
    except (ValueError, KeyError) as error:
        raise HTTPException(409, str(error))
    return public_short(store().get_short(short_id))


@router.post('/{collection_id}/shorts/{short_id}/settings', status_code=202)
def settings(collection_id: str, short_id: str, body: SettingsRequest):
    from .collection_generation import update_settings
    short_in(collection_id, short_id)
    try:
        update_settings(store(), context().STORE, collection_id, short_id, context().WORKER,
                        **body.model_dump(exclude_unset=True))
    except (ValueError, KeyError) as error:
        raise HTTPException(409, str(error))
    return public_short(store().get_short(short_id))


@router.post('/{collection_id}/shorts/{short_id}/decision')
def decide(collection_id: str, short_id: str, body: DecisionRequest):
    short_in(collection_id, short_id)
    try:
        return store().save_decision(short_id, body.revision_id, body.rating, body.notes,
                                     expected_revision=body.revision_id, acknowledge_warnings=body.acknowledge_warnings)
    except ValueError as error:
        raise HTTPException(409, str(error))
    except KeyError:
        raise HTTPException(409, 'This decision must apply to the displayed current revision. Refresh the short.')


@router.post('/{collection_id}/shorts/{short_id}/restore')
def restore(collection_id: str, short_id: str, body: RestoreRequest):
    short_in(collection_id, short_id)
    try:
        short = store().restore_revision(collection_id, short_id, **body.model_dump())
    except (ValueError, KeyError) as error:
        raise HTTPException(409, str(error))
    return public_short(short)


@router.post('/{collection_id}/exports', status_code=202)
def export_selected(collection_id: str, body: ExportRequest):
    from .collection_generation import export_revisions
    collection(collection_id)
    try:
        export_revisions(store(), context().STORE, collection_id, [item.model_dump() for item in body.revisions], context().WORKER)
    except (ValueError, KeyError):
        raise HTTPException(409, 'Select valid reviewed revisions from this collection.')
    return public(collection(collection_id), detail=True)


@router.get('/{collection_id}/artifacts/{artifact_id}/{kind}')
def artifact_file(collection_id: str, artifact_id: str, kind: Literal['video', 'subtitles', 'manifest']):
    collection(collection_id)
    try:
        artifact = store().get_artifact(artifact_id)
    except KeyError:
        raise HTTPException(404, 'Artifact not found.')
    if artifact['collection_id'] != collection_id:
        raise HTTPException(404, 'Artifact not found in this collection.')
    path = Path(artifact['document'][{'video': 'video_path', 'subtitles': 'srt_path', 'manifest': 'manifest_path'}[kind]]).resolve()
    if not path.is_relative_to(store().root / 'collection-artifacts') or not path.is_file():
        raise HTTPException(404, 'Saved artifact file is unavailable.')
    return FileResponse(path, media_type={'video': 'video/mp4', 'subtitles': 'text/plain', 'manifest': 'application/json'}[kind])


class MoreRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_revision: StrictStr = Field(min_length=1, max_length=100)
    request_id: StrictStr = Field(min_length=1, max_length=100)
    instruction: StrictStr = Field(default='', max_length=1000)


@router.post('/{collection_id}/shorts/{short_id}/more', status_code=202)
def more(collection_id: str, short_id: str, body: MoreRequest):
    from .collection_generation import more_like_short
    short_in(collection_id, short_id)
    if (message := _keys_message(context().KEYS)):
        raise HTTPException(422, message)
    try:
        more_like_short(store(), context().STORE, collection_id, short_id, context().WORKER, **body.model_dump())
    except (ValueError, KeyError, OSError) as error:
        raise HTTPException(409, str(error))
    return public(store().get_collection(collection_id), detail=True)


def _keys_message(keys):
    from .models.gateway import keys_message
    return keys_message(keys)
