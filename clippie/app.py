"""Local review workbench; studio work, bounded collection model/media lanes, and independent intake lanes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ConfigDict

from .store import Store, now
from . import intake, media, selection, telemetry
from .credentials import CredentialStore, CredentialStorageError
from .paths import app_version, data_root, resource_root

ROOT = Path(__file__).resolve().parents[1]
STORE = Store(data_root())
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='clippie')
CREDENTIALS = CredentialStore(ROOT)
SMOKE_TEST = os.getenv('CLIPPIE_SMOKE_TEST') == '1'
if SMOKE_TEST:
    # An isolated packaged-runtime check must not open the user's Keychain.
    from .credentials import NAMES
    KEYS = {name: '' for name in NAMES}
    CREDENTIALS.origins = {name: 'none' for name in NAMES}
else:
    KEYS = CREDENTIALS.load()
BUSY = {'queued', 'ingesting', 'transcribing', 'selecting', 'exporting'}


@asynccontextmanager
async def lifespan(app):
    from .collection_workflow import resume_preparation, stop_preparation
    from .collection_batches import resume_batches
    try:
        telemetry.recorder(STORE.root)
    except Exception:
        # Observability must not prevent the editor from starting.
        pass
    from .publish import sync as publish_sync
    from .storage_safety import complete_startup, background_resume_disabled, quarantine_restored_work
    quarantine_restored_work(STORE.root)
    complete_startup(STORE.root)  # finish local initialization before any dispatch
    recovery_stop = threading.Event()
    recovery_thread = None
    if not SMOKE_TEST:
        intake.recover(STORE)
        if not background_resume_disabled(STORE.root):
            from .studio_api import recover_jobs
            recover_jobs()
            def recovery_loop():
                import logging
                while not recovery_stop.wait(20):
                    if not background_resume_disabled(STORE.root):
                        try:
                            recover_jobs()
                        except Exception:
                            logging.getLogger('clippie.recovery').exception('Studio recovery pass failed')
            recovery_thread = threading.Thread(target=recovery_loop, name='clippie-recovery', daemon=True)
            recovery_thread.start()
            from . import lifecycle, lifecycle_api
            if not os.environ.get('CLIPPIE_DISABLE_LIFECYCLE_SWEEP'):
                lifecycle.start_background(lifecycle_api.make_ctx, recovery_stop, first_delay=3.0)
            resume_batches(STORE.root, STORE, WORKER)
            resume_preparation(STORE.root, STORE, WORKER)
            publish_sync.start_background(STORE.root)
    yield
    recovery_stop.set()
    if recovery_thread:
        recovery_thread.join(timeout=2)
    from .model_setup import shutdown as stop_model_setup
    stop_model_setup(STORE.root)
    publish_sync.stop_all()
    stop_preparation()
    WORKER.shutdown(wait=False, cancel_futures=True)
    intake.shutdown_lanes()
    telemetry.shutdown(STORE.root)


app = FastAPI(title='Clippie', lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    if request.url.path == '/api/settings':
        # Pydantic's default error payload includes the rejected input value.
        return JSONResponse({'detail': 'Use a supported API-key field with at most 4096 characters.'}, status_code=422)
    return await request_validation_exception_handler(request, error)


@app.middleware('http')
async def lifecycle_guard(request: Request, call_next):
    """Freed or trashed projects refuse edits, exports and generation; successful ones count as activity."""
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.url.path.startswith(('/api/studio/', '/api/collections/')):
        from anyio import to_thread
        from . import lifecycle_api
        refusal = await to_thread.run_sync(lifecycle_api.guard, request.method, request.url.path)
        if refusal:
            return JSONResponse({'detail': refusal[1]}, status_code=refusal[0])
        response = await call_next(request)
        if response.status_code < 400:
            try:
                project = await to_thread.run_sync(lifecycle_api.touched, request.method, request.url.path)
                if project:
                    await to_thread.run_sync(lifecycle_api.touch_project, project)
            except Exception:
                pass
        return response
    return await call_next(request)


@app.middleware('http')
async def local_origin(request: Request, call_next):
    if request.headers.get('host', '').split(':')[0] not in {'127.0.0.1', 'localhost', 'testserver'}:
        return JSONResponse({'detail': 'Clippie accepts local connections only.'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        return JSONResponse({'detail': 'Cross-origin requests are disabled.'}, status_code=403)
    origin_tag = request.headers.get('x-clippie-observation', 'qa' if request.url.hostname == 'testserver' else 'user')
    token = telemetry.OBSERVATION.set(origin_tag if origin_tag in {'user','qa','synthetic'} else 'user')
    try:
        response = await call_next(request)
        if response.status_code >= 400 and request.method in {'POST','PUT','PATCH'} and not request.url.path.startswith('/api/system/'):
            telemetry.event('request_rejected', http_status=response.status_code, reason='request_rejected')
    finally:
        telemetry.OBSERVATION.reset(token)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if request.url.path in {'/', '/studio'}:
        response.headers['Cache-Control'] = 'no-store'
    return response


class Options(BaseModel):
    model_config = ConfigDict(extra='forbid')
    brief: str = Field(default='Find self-contained, compelling insights with a clear setup and payoff.', max_length=8000)
    selector: str = Field(default='heuristic', pattern='^(heuristic|deepseek)$')
    min_duration: float = Field(default=25, ge=3, le=300)
    max_duration: float = Field(default=60, ge=3, le=300)
    max_clips: int = Field(default=5, ge=1, le=12)


class ClipRange(BaseModel):
    """Seconds of the web video to keep; the saved file starts at 0."""
    model_config = ConfigDict(extra='forbid')
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)

    def model_post_init(self, _context):
        if self.end <= self.start:
            raise ValueError('The range must end after it starts.')


class NewJob(Options):
    source: str = Field(min_length=1, max_length=4096)
    transcriber: str = Field(default='auto', pattern='^(auto|mlx|groq|import|none)$')
    transcript_path: str | None = Field(default=None, max_length=4096)
    language: str | None = Field(default=None, max_length=12)
    quality: str = Field(default='standard', pattern='^(standard|high|small)$')
    range: ClipRange | None = None


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    deepseek_key: str | None = Field(default=None, max_length=4096)
    groq_key: str | None = Field(default=None, max_length=4096)
    gemini_key: str | None = Field(default=None, max_length=4096)
    anthropic_key: str | None = Field(default=None, max_length=4096)
    openai_key: str | None = Field(default=None, max_length=4096)
    nvidia_key: str | None = Field(default=None, max_length=4096)
    openrouter_key: str | None = Field(default=None, max_length=4096)


def get_job(job_id):
    try:
        return STORE.get(job_id)
    except KeyError:
        raise HTTPException(404, 'Job not found')


def check_options(options):
    if options.max_duration < options.min_duration:
        raise HTTPException(422, 'Maximum duration must be at least the minimum duration.')
    if options.selector == 'deepseek' and not KEYS['deepseek']:
        raise HTTPException(422, 'Add your DeepSeek API key in Settings, or choose the heuristic baseline.')


def public(job):
    # Private paths remain local and are useful for provenance; no secrets are stored.
    result = dict(job)
    result['word_count'] = len(job.get('words') or [])
    result['media_url'] = f"/api/jobs/{job['id']}/media" if job.get('preview_path') else None
    result.update(intake.progress_view(job))
    return result


def clean_error(error):
    message = str(error)
    for key in KEYS.values():
        if key:
            message = message.replace(key, '[redacted]')
    return message[-1800:] or type(error).__name__


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


@telemetry.instrument('selection')
def do_select(job_id, options):
    job = STORE.update(job_id, status='selecting', progress='Finding self-contained candidate clips…', error=None)
    started = time.monotonic()
    result = selection.select_clips(job['words'], options.brief, provider=options.selector,
                                    min_duration=options.min_duration, max_duration=options.max_duration,
                                    max_clips=options.max_clips, api_key=KEYS['deepseek'] or None)
    clips = result['clips']
    run_id = uuid4().hex[:12]
    for clip in clips:
        clip['id'] = f"{run_id}-{clip['id']}"
        clip['original_start'] = clip['start']
        clip['original_end'] = clip['end']
    metrics = {**result.get('metrics', {}), 'selection_seconds': round(time.monotonic() - started, 2)}
    run = {'id': run_id, 'created_at': now(), 'brief': options.brief, 'provider': options.selector,
           'options': options.model_dump(), 'clips': clips, 'metrics': metrics,
           'warnings': result.get('warnings', [])}
    directory = STORE.directory(job_id)
    write_json(directory / f'run-{run_id}.json', {**run, 'raw': result.get('raw')})
    current = get_job(job_id)
    STORE.update(job_id, status='ready', progress=f'{len(clips)} candidates ready to review.', clips=clips,
                 runs=current.get('runs', []) + [run], brief=options.brief, selector=options.selector,
                 options=options.model_dump(), metrics={**current.get('metrics', {}), **metrics},
                 warnings=list(dict.fromkeys(current.get('warnings', []) + result.get('warnings', []))))


def intake_env():
    return intake.Env(store=STORE, keys=KEYS, select=do_select, clean=clean_error)


def process_job(job_id, request):
    """Intake for one source (see `intake`): runs on the network lane for a URL, else on the local lane."""
    intake.run(intake_env(), job_id, request)


def start_job(job_id, request, attempt=1):
    """Plan the stages, then queue the job on its first lane."""
    intake.Tracker.open(STORE, job_id).plan(intake.stages_for(request.source))
    run = telemetry.begin('ingest', source_id=job_id, transcriber=request.transcriber, planner=request.selector, attempt=attempt)
    if run:
        STORE.update(job_id, telemetry_run_id=run.id, telemetry_attempt=attempt)
    telemetry.submit(intake.lane_for(request.source).bind(job_id, 'fetch'), 'ingest', process_job, job_id, request,
                     telemetry_run=run)


def new_record(source, options):
    return {'id': uuid4().hex, 'title': Path(source).name if not source.startswith('http') else source,
            'source': source, 'source_path': None, 'duration': None, 'created_at': now(),
            'status': 'queued', 'progress': 'Waiting to start', 'error': None, 'stages': [], 'overall': 0.0,
            'brief': options.brief, 'selector': options.selector, 'transcriber': 'pending',
            'words': [], 'segments': [], 'clips': [], 'runs': [], 'exports': [], 'metrics': {}, 'warnings': [],
            'options': options.model_dump()}


@app.get('/api/health')
def health():
    ffmpeg = os.getenv('CLIPPIE_FFMPEG', 'ffmpeg')
    available = bool(shutil.which(ffmpeg))
    filters = subprocess.run([ffmpeg, '-hide_banner', '-filters'], capture_output=True, text=True, timeout=10).stdout if available else ''
    return {'version': app_version(), 'ffmpeg': available, 'ffprobe': bool(shutil.which(os.getenv('CLIPPIE_FFPROBE', 'ffprobe'))),
            'mlx_available': importlib.util.find_spec('mlx_whisper') is not None,
            'caption_burn_available': caption_overlay_support(filters),
            'local_models': local_models(),
            **settings()}


def caption_overlay_support(filters):
    # v3 renders captions to PNG with Pillow and composites with overlay. ASS
    # support is unrelated; requiring it made a working packaged renderer look
    # broken on first launch.
    names = {line.split()[1] for line in filters.splitlines() if len(line.split()) >= 2}
    return ({'overlay', 'format'} <= names and importlib.util.find_spec('PIL') is not None
            and (resource_root() / 'assets/fonts/Inter-Bold.ttf').is_file())


def local_models():
    """Presence of optional local model weights, with the command that prepares each one."""
    from . import analysis, local_editor, local_models as whisper
    semantic, editor = analysis.semantic_status(), local_editor.status()
    converted = Path(os.environ['CLIPPIE_WHISPER_MODEL']).expanduser() if os.getenv('CLIPPIE_WHISPER_MODEL') else whisper.converted_model_path()
    try:
        from .analyzers import faces
        face_model = faces.model_path()
    except Exception:
        face_model = STORE.root / 'models' / 'face_detection_yunet_2023mar.onnx'
    return {
        'whisper': {'label': 'Whisper (speech to text)', 'installed': importlib.util.find_spec('mlx_whisper') is not None,
                    'ready': (converted / 'weights.safetensors').is_file() and (converted / 'config.json').is_file(), 'path': str(converted),
                    'command': 'uv run --extra local python scripts/prepare_local_model.py'},
        'embeddings': {'label': f"{semantic['model']} (semantic search)", 'installed': semantic['installed'],
                       'ready': semantic['weights_ready'], 'path': str(analysis.model_directory()),
                       'command': 'uv run --extra semantic python scripts/prepare_embeddings.py'},
        'vision': {'label': 'YuNet (face tracking for reframing)',
                   'installed': importlib.util.find_spec('cv2') is not None,
                   'ready': face_model.is_file(), 'path': str(face_model),
                   'command': 'uv run --extra vision python scripts/prepare_vision.py'},
        'editor': {'label': f"{editor['model']} (local editor)", 'installed': editor['installed'],
                   'ready': editor['weights_ready'], 'path': str(local_editor.model_directory()),
                   'command': 'uv run --extra editor python scripts/prepare_editor.py'},
    }


@app.get('/guide')
def guide():
    return FileResponse(resource_root().parent / 'README.md', media_type='text/plain; charset=utf-8')


@app.get('/api/settings')
def settings():
    from .gemini import MODEL_ID
    from .models.providers import vertex
    return {'deepseek_configured': bool(KEYS['deepseek']), 'groq_configured': bool(KEYS['groq']),
            'vertex_configured': vertex.configured(),
            'gemini_configured': bool(KEYS.get('gemini')), 'gemini_model': MODEL_ID,
            'anthropic_configured': bool(KEYS.get('anthropic')), 'openai_configured': bool(KEYS.get('openai')),
            'nvidia_configured': bool(KEYS.get('nvidia')), 'openrouter_configured': bool(KEYS.get('openrouter')),
            'credential_storage': dict(CREDENTIALS.origins), 'credential_warning': CREDENTIALS.warning}


@app.post('/api/settings')
def set_settings(body: Credentials):
    for name in ('deepseek', 'groq', 'gemini', 'anthropic', 'openai', 'nvidia', 'openrouter'):
        value = getattr(body, name + '_key')
        if value is not None:
            try:
                KEYS[name] = CREDENTIALS.save(name, value)
            except (CredentialStorageError, ValueError) as error:
                raise HTTPException(503 if isinstance(error, CredentialStorageError) else 422, str(error)) from None
    return settings()


@app.delete('/api/settings/{provider}')
def remove_key(provider: str):
    if provider not in KEYS:
        raise HTTPException(404, 'Unknown credential provider.')
    try:
        KEYS[provider] = CREDENTIALS.remove(provider)
    except CredentialStorageError as error:
        raise HTTPException(503, str(error)) from None
    return settings()


@app.get('/api/jobs')
def jobs():
    return [{key: value for key, value in public(job).items() if key not in {'words', 'segments', 'runs', 'exports'}} for job in STORE.all()]


@app.post('/api/jobs', status_code=202)
def create_job(body: NewJob):
    check_options(body)
    if body.transcriber == 'groq' and not KEYS['groq']:
        raise HTTPException(422, 'Add a Groq key in Settings, or choose local transcription.')
    if body.transcriber == 'import' and not body.transcript_path:
        raise HTTPException(422, 'Provide the local JSON or SRT transcript path.')
    source = body.source.strip()
    if not source.startswith(('https://', 'http://')) and not Path(source).expanduser().is_file():
        raise HTTPException(422, 'Source file does not exist. Paste an absolute local path or a video URL.')
    body.source = source
    job = STORE.save(new_record(source, body))
    start_job(job['id'], body)
    return public(STORE.get(job['id']))


def retry_job(job_id):
    """Run a failed or cancelled source again. Downloads resume, finished files and transcripts are reused."""
    job = get_job(job_id)
    if job['status'] in BUSY:
        raise HTTPException(409, 'This source is already being processed.')
    if job['status'] not in {'failed', 'cancelled'}:
        raise HTTPException(409, 'Only a failed or cancelled source can be retried.')
    source = job['source']
    if not source.startswith(('https://', 'http://')) and not Path(source).expanduser().is_file():
        raise HTTPException(422, 'The source file is no longer on this computer.')
    try:
        body = NewJob(**{**(job.get('options') or {}), 'source': source})
    except ValueError:
        raise HTTPException(422, 'This source cannot be retried; add it again.') from None
    attempt = int(job.get('telemetry_attempt') or 1) + 1
    STORE.update(job_id, status='queued', error=None, progress='Waiting to start')
    start_job(job_id, body, attempt)
    return STORE.get(job_id)


def cancel_job(job_id):
    get_job(job_id)
    try:
        return intake.cancel(intake_env(), job_id)
    except intake.NotCancellable as error:
        raise HTTPException(409, str(error)) from None


@app.get('/api/jobs/{job_id}')
def job_detail(job_id: str):
    return public(get_job(job_id))


@app.get('/api/jobs/{job_id}/media')
def job_media(job_id: str):
    path = get_job(job_id).get('preview_path')
    if not path or not Path(path).is_file():
        raise HTTPException(404, 'Preview is not ready yet.')
    return FileResponse(path, media_type='video/mp4')


@app.get('/favicon.ico')
def favicon():
    return Response(status_code=204)


from .telemetry_api import router as telemetry_router
app.include_router(telemetry_router)
from .collection_api import router as collection_router
app.include_router(collection_router)
from .collection_batches import router as collection_batches_router
app.include_router(collection_batches_router)
from .collection_preferences import router as collection_preferences_router
app.include_router(collection_preferences_router)
from .models_api import router as models_router
app.include_router(models_router)
from .model_setup_api import router as model_setup_router
app.include_router(model_setup_router)
from .recovery_api import router as recovery_router
app.include_router(recovery_router)
from .lifecycle_api import router as lifecycle_router
app.include_router(lifecycle_router)
from .studio_api import router as studio_router, static_router as studio_static_router
app.include_router(studio_router)
app.include_router(studio_static_router)
from .publish_api import router as publish_router
app.include_router(publish_router)


@app.get('/static/{path:path}')
def static_missing(path: str):
    raise HTTPException(404, 'Not found.')


@app.get('/')
def index():
    from .studio_api import index_page
    return index_page()


def sources_for(ids, require_speech=True):
    """Resolve ready source jobs for the collection engine; 422 when one is missing or unfinished."""
    if len(ids) != len(set(ids)):
        raise HTTPException(422, 'Select each source only once.')
    sources = {}
    for source_id in ids:
        job = get_job(source_id)
        if job['status'] in BUSY or job['status'] == 'cancelled' or not job.get('source_path') or not Path(job['source_path']).is_file():
            raise HTTPException(422, f"Source {job.get('title', source_id)} is not ready.")
        if not job.get('duration'):
            raise HTTPException(422, 'Source duration is missing.')
        sources[source_id] = job
    if require_speech and not any(job.get('words') for job in sources.values()):
        raise HTTPException(422, 'Include at least one source with a transcript for the original speech track.')
    return sources
