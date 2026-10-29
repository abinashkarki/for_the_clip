"""Shared source discovery and independent, bounded editorial task chains."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import copy
from contextvars import copy_context
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from . import (clip_scoring, collection_editor as editor, collection_provider as provider, source_evidence, telemetry,
               collection_visuals, studio_engine as engine, studio_timeline as tl, v3_compiler)
from .collection_runner import run_one

REMOTE_WORK = ThreadPoolExecutor(max_workers=2, thread_name_prefix='collection-model')
LOCAL_WORK = ThreadPoolExecutor(max_workers=1, thread_name_prefix='collection-media')
LOCAL_STAGES = frozenset({'compile_short', 'render_preview', 'render_export', 'apply_settings'})

DEFAULT_CALLS = 16
DEFAULT_SECONDS = 600
TRANSIENT = {'provider_network', 'provider_timeout', 'provider_service', 'provider_retry_budget'}
# A model answer that failed validation gets one more attempt automatically: answers vary between calls.
# Discovery is excluded because its calls are checkpointed, so a retry would replay the same answer.
REASK = {'editor_invalid_output'}
CHECKPOINTED = {'discover', 'discover_more'}


class EditorialValidationError(ValueError):
    category = 'editor_invalid_output'


class EditorialVersionChanged(ValueError):
    category = 'editor_version_changed'


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _material(storage, sources, run):
    jobs, evidence = {}, {}
    for source_id, version in run['configuration']['source_versions'].items():
        source = sources.get(source_id)
        if source_evidence.evidence_key(source) != version:
            raise ValueError('Source evidence changed during this run.')
        jobs[source_id] = source
        evidence[source_id] = source_evidence.prepare_evidence(source, storage.root / 'source-evidence')
    for source_id, snapshot in run['configuration'].get('picture_versions', {}).items():
        source = sources.get(source_id)
        if collection_visuals.source_snapshot(source) != snapshot:
            raise ValueError('Picture-only source media changed during this run.')
        jobs[source_id] = source
    return jobs, evidence


def start_generation(storage, sources, collection_id, executor, *, request_id='initial',
                     calls=DEFAULT_CALLS, seconds=DEFAULT_SECONDS, dispatch_work=True):
    if type(calls) is not int or not 1 <= calls <= 24 or type(seconds) is not int or not 30 <= seconds <= 900:
        raise ValueError('Choose 1–24 requests and a deadline of 30–900 seconds.')
    collection = storage.get_collection(collection_id)
    constraints = collection['constraints']
    versions = {sid: source_evidence.evidence_key(sources.get(sid)) for sid in collection['source_ids']
                if constraints.get('source_roles', {}).get(sid, 'primary') == 'primary'}
    existing = storage.get_run(collection['current_run']) if collection.get('current_run') else None
    # Plan the same packet envelopes and transcript windows used by execution.
    # Each discovery window is a separately admitted volume invocation; dense
    # packet reservations can also exceed the standard map envelope.
    default_tokens = None
    if calls == DEFAULT_CALLS:  # a smaller explicit request ceiling remains binding
        prepared = {sid: source_evidence.prepare_evidence(sources.get(sid), storage.root / 'source-evidence')
                    for sid in versions}
        packets = [packet for evidence in prepared.values() for packet in source_evidence.packets(evidence)]
        windows = len(editor._windows(editor._transcript(editor._artifacts(prepared), constraints),
                                      editor.DISCOVERY_WINDOW_CHARS))
        # The blind opening judge, then review, one repair and re-review follow discovery. Keep three spare
        # calls with volume-sized envelopes for recovery, retaining the minimum allowance.
        planned_calls = len(packets) + windows + 4
        calls = max(calls, planned_calls + 3)
        default_tokens = (sum(_map_reservation(packet) for packet in packets)
                          + (windows + 4) * provider.VOLUME_RESERVATION_TOKENS
                          + (calls - planned_calls) * provider.VOLUME_RESERVATION_TOKENS)
        seconds = max(seconds, 240 + 20 * len(packets))
    deadline = (existing['configuration']['deadline'] if existing and existing['request_id'] == request_id
                else time.time() + seconds)
    configuration = {'provider': 'gemini', 'source_versions': versions, 'constraints': constraints,
                     'deadline': deadline, 'calls': calls, 'seconds': seconds,
                     'prompt_version': editor.PROMPT_VERSION}
    pictures = {sid: collection_visuals.source_snapshot(sources.get(sid)) for sid in collection['source_ids']
                if constraints.get('source_roles', {}).get(sid, 'primary') == 'picture_only'}
    if pictures:
        configuration['picture_versions'] = pictures
    run = storage.start_run(collection_id, configuration, request_id)
    if collection['budget']['calls'] == 0 and collection['budget']['tokens'] == 0:
        if default_tokens is None:  # Preserve the narrower, explicitly supplied request allowance.
            batched = 4 * (provider.VOLUME_RESERVATION_TOKENS - provider.RESERVATION_TOKENS)
            default_tokens = calls * provider.RESERVATION_TOKENS + batched
        storage.extend_budget(collection_id, calls=calls, tokens=default_tokens,
                              request_id='generation-' + request_id)
    if run['status'] == 'active':
        _seed(storage, sources, run)
        if dispatch_work:
            from .collection_workflow import dispatch
            dispatch(storage, sources, collection_id, executor)
    return storage.get_run(run['id'])


def _map_reservation(packet):
    """Room for this packet's real transport (dense speech can exceed the normal envelope) plus its answer."""
    from .collection_editor import _compact_fragment
    body = len(json.dumps([_compact_fragment(f) for f in packet['fragments']], ensure_ascii=False).encode('utf-8'))
    return max(provider.RESERVATION_TOKENS, body + 12_288)


def _enqueue(storage, run, stage, identity, *, dependencies=(), short_id=None, paid=True, volume=False, tokens=None, **payload):
    version = key([run['id'], stage, identity])
    tokens = tokens or (provider.VOLUME_RESERVATION_TOKENS if volume else provider.RESERVATION_TOKENS)
    return storage.enqueue_task(run['collection_id'], stage, version, version, short_id=short_id,
        dependencies=list(dependencies), reserved_tokens=tokens if paid else 0, reserved_calls=1 if paid else 0,
        payload={'run_id': run['id'], 'deadline': run['configuration']['deadline'], **payload})


def _seed(storage, sources, run):
    _, evidence = _material(storage, sources, run)
    maps = []
    for source_id, prepared in evidence.items():
        for packet in source_evidence.packets(prepared):
            maps.append(_enqueue(storage, run, 'map_source', packet['id'], tokens=_map_reservation(packet),
                                 source_id=source_id, packet_id=packet['id']))
    _enqueue(storage, run, 'discover', 'slate', dependencies=[task['id'] for task in maps], volume=True)


def _invoke(ctx):
    from . import app
    def invoke(stage, prompt, schema, max_tokens, *, pictures=(), volume=False):
        if ctx.task['payload'].get('run_id'):
            run = ctx.store.get_run(ctx.task['payload']['run_id'])
            ctx.task['payload']['deadline'] = max(ctx.task['payload']['deadline'], run.get('deadline', 0))
        # Discovery remains sequential: prompt identity includes already_proposed,
        # fixed history, source/evidence input version and exact route options.
        checkpoint_id = None
        # Every call a discovery task makes (its windows and the opening judge) is checkpointed for retries.
        if stage == 'discover' or ctx.task['stage'] in ('discover', 'discover_more'):
            from .models import gateway
            checkpoint_id = key([ctx.task['id'], ctx.task['input_version'], stage, prompt, schema,
                                 max_tokens, pictures, volume, gateway.routing()])
            reused = ctx.store.get_stage_checkpoint(ctx.task['id'], checkpoint_id)
            if reused is not None:
                ctx.checkpoint()
                ctx.invocation_reused = True
                return {**reused, 'usage': {}, 'provider_requests': 0}
        ctx.invocation_sent = True
        response = provider.invoke(ctx, app.KEYS.get('gemini', ''), stage, prompt, schema, max_tokens,
                                   **({'pictures': pictures} if pictures else {}), **({'volume': True} if volume else {}))
        if checkpoint_id:
            ctx.store.save_stage_checkpoint(ctx.task['id'], ctx.task['lease_token'], checkpoint_id, response)
        ctx.last_proposal = response.get('proposal')
        ctx.last_producer = {name: response.get(name) for name in ('provider', 'model', 'revision', 'call_id')}
        from .models import gateway
        ctx.last_producer['options'] = gateway.route_options(stage, response.get('provider'), response.get('model'))
        return response
    return invoke


def _cached_map(storage, ctx, prepared, packet, source):
    from .models import gateway
    from . import source_visuals
    start, end = packet['start'], packet['end']
    times = list(dict.fromkeys([start + (end-start) * .25, start + (end-start) * .75]))
    with telemetry.stage('source_evidence', collection_id=ctx.task['collection_id'], task_id=ctx.task['id'],
                         source_id=source['id']):
        visuals = source_visuals.prepare_samples(source, storage.root / 'source-visuals', times, process_owner=ctx)
        telemetry.attributes(cache_kind='evidence', cache='hit' if visuals['cache_status'] == 'hit' else 'miss')
    route = gateway.routing()
    identity = key([route, editor.MAP_PROMPT_VERSION, prepared['cache_artifact_key'], packet['id'],
                    visuals['cache_artifact_key']])
    root = storage.root / 'topic-maps'
    root.mkdir(parents=True, exist_ok=True)
    path = root / (identity + '.json')
    with (root / (identity + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ctx.checkpoint()
        if path.exists():
            document = json.loads(path.read_text())
            if document.get('cache_key') != identity:
                raise ValueError('Cached source map identity mismatch.')
            telemetry.attributes(cache='hit')
            return document['map'], True
        pictures = []
        for sample in visuals['artifact']['samples']:
            pictures.append(collection_visuals.picture_payload(sample))
        document = editor.map_packet(packet, prepared, _invoke(ctx), pictures=pictures)
        document['producer'] = getattr(ctx, 'last_producer', {})
        document['visual_evidence'] = {'artifact_key': visuals['cache_artifact_key'],
            'sample_ids': document['visual_sample_ids'], 'sampled_count': len(pictures),
            'samples': [{k: sample[k] for k in ('id', 'source_id', 'at', 'width', 'height', 'sha256')}
                        for sample in visuals['artifact']['samples']],
            'omitted_count': document['visual_omitted_count'], 'sampling': 'two stills per transcript packet',
            'transport_profile': {'max_width': 240, 'max_height': 135, 'jpeg_quality_ceiling': 45,
                                  'jpeg_byte_cap': 12_000}}
        ctx.checkpoint()
        with tempfile.NamedTemporaryFile('w', dir=root, delete=False) as stream:
            temporary = stream.name
            json.dump({'cache_key': identity, 'map': document}, stream, allow_nan=False)
        try:
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
        telemetry.attributes(cache='miss')
        return document, False


def _result(document, *, cached=False):
    result = {'result': document, 'actual_calls': 0 if cached else 1}
    usage = document.get('model_usage', {})
    if not cached and type(usage.get('total_tokens')) is int:
        result['actual_tokens'] = usage['total_tokens']
    elif cached:
        result['actual_tokens'] = 0
    return result


def _idea_history(storage, collection_id):
    history = [copy.deepcopy(r['document']) for r in storage.list_candidates(collection_id)]
    for short in storage.list_shorts(collection_id):
        if short.get('current_revision'):
            revision = storage.get_revision(short['current_revision'])
            plan = revision['document']['plan']
            history.append({'id': 'revision-' + revision['id'], 'angle': plan.get('angle', ''),
                            'unit_ids': plan.get('unit_ids', []), 'disposition': 'current_revision',
                            'decisions': [{'rating': d['rating'], 'notes': d.get('notes', '')}
                                          for d in storage.list_decisions(short['id'])]})
    return history


def more_like_short(storage, sources, collection_id, short_id, executor, *, base_revision, request_id, instruction=''):
    identity = key(['more', request_id])
    existing = next((t for t in storage.list_tasks(collection_id) if t['idempotency_key'] == identity), None)
    if existing:
        if any(existing['payload'].get(k) != v for k, v in
               {'base_short_id': short_id, 'base_revision': base_revision, 'instruction': instruction}.items()):
            raise ValueError('More request ID reused with different inputs.')
        return existing
    collection = storage.get_collection(collection_id)
    short = storage.get_short(short_id)
    if short['collection_id'] != collection_id or short['current_revision'] != base_revision:
        raise ValueError('The selected revision changed.')
    revision = storage.get_revision(base_revision)
    document = revision['document']
    if document.get('technical') != 'pass' or document.get('editorial', {}).get('status') != 'pass':
        raise ValueError('Select a passing revision.')
    run = storage.get_run(collection['current_run'])
    if short.get('metadata', {}).get('run_id') != run['id']:
        raise ValueError('Select a short from the current run.')
    _material(storage, sources, run)
    maps = [t for t in storage.list_tasks(collection_id) if t['stage'] == 'map_source'
            and t['payload'].get('run_id') == run['id'] and t['status'] == 'completed']
    if not maps:
        raise ValueError('Current source maps are unavailable.')
    if sum(t['stage'] == 'discover_more' for t in storage.list_tasks(collection_id)) >= 12:
        raise ValueError('Additional discovery limit reached for this collection.')
    constraints = copy.deepcopy(run['configuration']['constraints'])
    base = document['plan']
    for name in ('edit_format', 'captions', 'caption_style', 'canvas_style', 'framing', 'shot_framing', 'aspect_ratio'):
        if name in base.get('constraints', {}):
            constraints[name] = copy.deepcopy(base['constraints'][name])
    constraints.update(max_shorts=1, focus='Find a DIFFERENT central claim, never a paraphrase. Related focus: '
                       + base.get('angle', '') + '. User direction: ' + instruction
                       + '. Collection focus: ' + constraints.get('focus', ''))
    task = storage.enqueue_task(collection_id, 'discover_more', identity, identity,
        dependencies=[t['id'] for t in maps], reserved_tokens=provider.VOLUME_RESERVATION_TOKENS, reserved_calls=1,
        payload={'run_id': run['id'], 'deadline': time.time() + DEFAULT_SECONDS,
                 'base_short_id': short_id, 'base_revision': base_revision, 'instruction': instruction,
                 'constraints': constraints})
    from .collection_workflow import dispatch
    dispatch(storage, sources, collection_id, executor)
    return task


def _handlers(storage, sources, run):
    collection_id = run['collection_id']
    constraints = run['configuration']['constraints']

    def map_source(ctx):
        jobs, evidence = _material(storage, sources, run)
        prepared = evidence[ctx.task['payload']['source_id']]
        packet = next(p for p in source_evidence.packets(prepared) if p['id'] == ctx.task['payload']['packet_id'])
        mapped, cached = _cached_map(storage, ctx, prepared, packet, jobs[ctx.task['payload']['source_id']])
        return _result(mapped, cached=cached)

    def discover(ctx):
        _, evidence = _material(storage, sources, run)
        if 'committed_candidates' in ctx.task:
            records = storage.list_candidates(collection_id, ctx.task['input_version'])
            slate = {'candidates': [r['document'] for r in records if r['document'].get('disposition') == 'selected'],
                     'rejected': [], 'explanation': 'Recovered saved selection.', 'model_usage': {}}
            slate = ctx.task.get('committed_slate', slate)
            cached = True
        else:
            maps = [storage.get_task(identity)['result'] for identity in ctx.task['dependencies']]
            history = [record['document'] for record in storage.list_candidates(collection_id)]
            slate = editor.discover(maps, evidence, ctx.task['payload'].get('constraints', constraints),
                                    _idea_history(storage, collection_id), _invoke(ctx))
            if ctx.task['stage'] == 'discover_more' and not slate['candidates']:
                slate['explanation'] = 'No distinct supported idea. ' + slate.get('explanation', '')
            pool = slate.get('pool', [{**candidate, 'disposition': 'selected'} for candidate in slate['candidates']])
            storage.save_candidates(collection_id, pool, ctx.task['input_version'], ctx.task['id'], ctx.task['lease_token'], slate=slate)
            cached = False
        for candidate in slate['candidates']:
            storage.ensure_short(collection_id, candidate['id'],
                metadata={'title': candidate['title'], 'angle': candidate['angle'], 'run_id': run['id'],
                          **{k: candidate[k] for k in ('score', 'score_reasons', 'score_components', 'score_version',
                                                       'hook_line', 'edit_format', 'duration', 'post_title',
                                                       'post_description', 'hashtags', 'hook_judge') if k in candidate},
                          **({'cold_open': True} if candidate.get('cold_open') else {}),
                          **({'more_task': ctx.task['id']} if ctx.task['stage'] == 'discover_more' else {})},
                task_id=ctx.task['id'], lease_token=ctx.task['lease_token'], input_version=ctx.task['input_version'])
        return _result(slate, cached=cached)

    def plan(ctx):
        jobs, evidence = _material(storage, sources, run)
        payload = ctx.task['payload']
        short = storage.get_short(ctx.task['short_id'])
        candidate = storage.get_candidate(collection_id, short['candidate_record_id'])['document']
        base = storage.get_task(payload['base_task'])['result'] if payload.get('base_task') else payload.get('base_plan')
        effective = payload.get('constraints', constraints)
        visual_samples, retrieval = collection_visuals.for_candidate(storage.root, sources.root, jobs, effective,
                                                                     candidate, process_owner=ctx)
        document = editor.plan_short(candidate, evidence, jobs, effective, _invoke(ctx),
                                     instruction=payload.get('instruction', ''), base_plan=base,
                                     visual_samples=visual_samples)
        document['visual_retrieval'] = retrieval
        return _result(document)

    def review(ctx):
        jobs, evidence = _material(storage, sources, run)
        payload = ctx.task['payload']
        plan = storage.get_task(payload['plan_task'])['result']
        review_constraints = copy.deepcopy(plan.get('constraints', constraints))
        if payload.get('more_task'):
            own = storage.get_candidate(collection_id, storage.get_short(ctx.task['short_id'])['candidate_record_id'])['document']['id']
            review_constraints['novelty_history'] = [h for h in _idea_history(storage, collection_id) if h.get('id') != own]
            review_constraints['novelty_requirement'] = 'Reject paraphrases or the same central claim as any non-deferred history, including current sibling revisions.'
        review = editor.review_short(plan, evidence, jobs, review_constraints, _invoke(ctx),
                                     visual_samples=collection_visuals.for_plan(storage.root, plan))
        if review['status'] == 'pass' and payload.get('more_task'):
            others = [h for h in _idea_history(storage, collection_id)
                      if h.get('id') != storage.get_candidate(collection_id, storage.get_short(ctx.task['short_id'])['candidate_record_id'])['document']['id']]
            if editor.repeated_idea(plan, others):
                review.update(status='needs_revision', issues=[{'category': 'novelty', 'severity': 'critical', 'detail': 'No distinct supported idea: proposed edit repeats previous evidence or angle.', 'unit_ids': plan['unit_ids']}])
        inspection_only = review['status'] in {'needs_revision', 'uncertain'} and payload.get('round') == 1
        if review['status'] == 'pass' or inspection_only:
            _material(storage, sources, run)  # Recheck source versions after the model call.
            revision = storage.append_revision(ctx.task['short_id'],
                {'plan': plan, 'timeline': plan['timeline'], 'editorial': review,
                 'technical': 'pass', 'human_review': 'unreviewed', 'artifact_state': 'plan',
                 **({'inspection_only': True} if inspection_only else {})},
                expected_revision=payload.get('base_revision'), task_id=ctx.task['id'],
                lease_token=ctx.task['lease_token'], input_version=ctx.task['input_version'], promote=False)
            review['revision_id'] = revision['id']
        return _result(review)

    def _slate_candidates(payload):
        slate = storage.get_task(payload['discover_task'])['result']
        return {c['id']: copy.deepcopy(c) for c in slate['candidates']}

    def _batch_items(payload):
        candidates = _slate_candidates(payload)
        if payload.get('repair_task'):
            repairs = storage.get_task(payload['repair_task'])['result']['repairs']
            return [{**candidates[cid], **repair} for cid, repair in repairs.items() if cid in candidates]
        return list(candidates.values())

    def review_batch(ctx):
        jobs, evidence = _material(storage, sources, run)
        items = _batch_items(ctx.task['payload'])
        review = editor.review_batch(items, evidence, jobs, constraints, _invoke(ctx))
        review['round'] = ctx.task['payload']['round']
        return _result(review)

    def repair_batch(ctx):
        jobs, evidence = _material(storage, sources, run)
        payload = ctx.task['payload']
        candidates = _slate_candidates(payload)
        results = storage.get_task(payload['review_task'])['result']['results']
        failed = [{**candidates[cid], 'issues': result['issues']} for cid, result in results.items()
                  if result['status'] != 'pass' and cid in candidates]
        return _result(editor.repair_batch(failed, evidence, jobs, constraints, _invoke(ctx)))

    def compile_short(ctx):
        """Local, model-free: candidate evidence IDs to a reviewed timeline v3 revision."""
        jobs, evidence = _material(storage, sources, run)
        payload = ctx.task['payload']
        short = storage.get_short(ctx.task['short_id'])
        candidate = copy.deepcopy(storage.get_candidate(collection_id, short['candidate_record_id'])['document'])
        review_task = storage.get_task(payload['review_task'])
        verdict = review_task['result']['results'][candidate['id']]
        if payload.get('repair_task'):
            candidate.update(storage.get_task(payload['repair_task'])['result']['repairs'].get(candidate['id'], {}))
        options, kit = v3_compiler.read_options(sources.root, collection_id)
        compiled = v3_compiler.compile_candidate(candidate, evidence, jobs, constraints, options, kit=kit,
                                                 cache_dir=sources.root / 'analysis-v3')
        ctx.checkpoint()
        _material(storage, sources, run)  # Recheck source versions after the local compile.
        length = engine.duration(compiled['timeline_v3'])
        rng = clip_scoring.preferred_range(constraints.get('max_duration', 60), constraints.get('preferred_duration'))
        scored = clip_scoring.score(candidate['components'], length, rng)
        held = verdict['status'] != 'pass'
        editorial = {'schema_version': 1, 'status': 'needs_revision' if held else 'pass', 'issues': verdict['issues'],
                     'review_task': review_task['id'], 'review_round': review_task['result'].get('round', 0),
                     'evidence_provenance': compiled['plan']['evidence_provenance'],
                     'model_usage': review_task['result'].get('model_usage', {}),
                     **({'repaired': True} if payload.get('repair_task') and not held else {})}
        document = {'plan': compiled['plan'], 'timeline': compiled['timeline'], 'timeline_v3': compiled['timeline_v3'],
                    'editorial': editorial, 'technical': 'pass', 'human_review': 'unreviewed',
                    'artifact_state': 'timeline', 'origin': 'generated', 'compile': compiled['notes'],
                    'score': {'score': scored['score'], 'score_reasons': scored['reasons'],
                              'score_components': scored['components'], 'score_version': scored['score_version'],
                              'duration': length, 'preferred_range': list(rng)},
                    **({'inspection_only': True} if held else {})}
        document['v2_digest'] = tl.v2_digest(document)
        revision = storage.append_revision(ctx.task['short_id'], document, expected_revision=None, task_id=ctx.task['id'],
                                           lease_token=ctx.task['lease_token'], input_version=ctx.task['input_version'])
        return {'result': {'revision_id': revision['id'], 'held': held, 'score': scored['score']},
                'actual_tokens': 0, 'actual_calls': 0}

    def media(ctx):
        from .collection_media import render_revision
        if ctx.task.get('committed_artifact'):
            artifact = storage.get_artifact(ctx.task['committed_artifact'])
            return {'result': {'artifact_id': artifact['id'], 'revision_id': artifact['revision_id']},
                    'actual_tokens': 0, 'actual_calls': 0}
        revision = storage.get_revision(ctx.task['payload']['revision_id'])
        kind = 'preview' if ctx.task['stage'] == 'render_preview' else 'export'
        jobs = _revision_sources(storage, sources, revision)
        if kind == 'export':
            storage.export_review_state(revision['id'], require=True)
        document = render_revision(ctx, revision, jobs, storage.root, kind,
            export_decisions=storage.list_decisions(revision['short_id'], revision['id']) if kind == 'export' else ())
        _revision_sources(storage, sources, revision)  # Source replacement during a render cannot publish.
        artifact = storage.commit_artifact(ctx.task['short_id'], revision['id'], kind, document,
                                          ctx.task['id'], ctx.task['lease_token'], ctx.task['input_version'])
        return {'result': {'artifact_id': artifact['id'], 'revision_id': revision['id']},
                'actual_tokens': 0, 'actual_calls': 0}

    def apply_settings(ctx):
        revision = storage.get_revision(ctx.task['payload']['base_revision'])
        jobs = _revision_sources(storage, sources, revision)
        evidence = {}
        for sid, fingerprint in revision['document']['plan']['evidence_provenance']['sources'].items():
            evidence[sid] = json.loads((storage.root / 'source-evidence' / f'evidence-{fingerprint}.json').read_text())
        plan = editor.apply_settings(revision['document']['plan'], evidence, jobs, ctx.task['payload']['settings'])
        document = copy.deepcopy(revision['document'])
        document.update(plan=plan, timeline=plan['timeline'], human_review='unreviewed', artifact_state='plan')
        document['editorial']['carried_review'] = {
            'base_revision': revision['id'], 'scope': 'Unchanged speech order and source times only; new visual settings are user-authored and have not received model visual review.'}
        saved = storage.append_revision(ctx.task['short_id'], document,
            expected_revision=revision['id'], task_id=ctx.task['id'], lease_token=ctx.task['lease_token'],
            input_version=ctx.task['input_version'], promote=False)
        return {'result': {'revision_id': saved['id']}, 'actual_tokens': 0, 'actual_calls': 0}

    from .collection_workflow import preparation_handler
    handlers = {'prepare_source': preparation_handler(storage, sources, collection_id),
            'map_source': map_source, 'discover': discover, 'discover_more': discover, 'plan_short': plan, 'review_short': review,
            'review_batch': review_batch, 'repair_batch': repair_batch, 'compile_short': compile_short,
            'render_preview': media, 'render_export': media, 'apply_settings': apply_settings}
    def guarded(function):
        def call(ctx):
            try:
                if (ctx.task['reservation']['calls'] and
                        run['configuration'].get('prompt_version') != editor.PROMPT_VERSION):
                    raise EditorialVersionChanged('Editor changed since this run started. Retry generation to use the current version.')
                storage.start_execution_clock(run['id'])
                with telemetry.stage('collection_task', fatal=False, task_id=ctx.task['id'],
                                     collection_id=collection_id, short_id=ctx.task.get('short_id')):
                    output = function(ctx)
                if getattr(ctx, 'invocation_reused', False) and not getattr(ctx, 'invocation_sent', False):
                    output.update(actual_calls=0, actual_tokens=0)
                return output
            except ValueError as error:
                # Content-bearing local diagnostics are separate from metadata telemetry.
                root = storage.root / 'collection-diagnostics'
                root.mkdir(exist_ok=True)
                path = root / f'{ctx.task["id"]}-{ctx.task["lease_token"]}.json'
                with path.open('w') as stream:
                    os.chmod(path, 0o600)
                    json.dump({'stage': ctx.task['stage'], 'validation_error': str(error)[:1000],
                               'proposal': getattr(ctx, 'last_proposal', None)}, stream, allow_nan=False)
                if getattr(ctx, 'last_proposal', None) is not None:
                    raise EditorialValidationError('The proposed edit failed validation.') from None
                raise
        return call
    return {stage: guarded(function) for stage, function in handlers.items()}


def _revision_sources(storage, sources, revision):
    """Use the revision's saved transcript version even after an ASR correction."""
    jobs = {}
    for source_id, fingerprint in revision['document']['plan']['evidence_provenance']['sources'].items():
        source = dict(sources.get(source_id))
        artifact = json.loads((storage.root / 'source-evidence' / f'evidence-{fingerprint}.json').read_text())
        identity = artifact['manifest'].get('media_identity')
        if identity:
            stat = Path(source['source_path']).resolve().stat()
            if any(identity.get(name) != value for name, value in {
                    'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'inode': stat.st_ino, 'device': stat.st_dev}.items()):
                raise ValueError('The original source media changed.')
        source.update(artifact['transcript'])
        jobs[source_id] = source
    for evidence in revision['document']['plan'].get('visual_evidence', []):
        source_id = evidence['source_id']
        source = dict(sources.get(source_id))
        if collection_visuals.source_snapshot(source)['media_identity'] != evidence['media_identity']:
            raise ValueError('The original supporting-picture media changed.')
        jobs.setdefault(source_id, source)
    return jobs


def _advance_batched(storage, run, discovery):
    """Batched review, one bounded title/hook repair, then local compile per short; no per-clip model call."""
    shorts = {short['candidate_id']: short for short in storage.list_shorts(run['collection_id'])
              if short.get('metadata', {}).get('run_id') == run['id'] and not short.get('metadata', {}).get('more_task')}
    if not shorts:
        return

    def compile_for(candidate_id, review, repair=None):
        _enqueue(storage, run, 'compile_short', ['compile', candidate_id], paid=False, short_id=shorts[candidate_id]['id'],
                 dependencies=[review['id']] + ([repair['id']] if repair else []), candidate_id=candidate_id,
                 review_task=review['id'], **({'repair_task': repair['id']} if repair else {}))

    first = _enqueue(storage, run, 'review_batch', 'round0', dependencies=[discovery['id']], volume=True,
                     round=0, discover_task=discovery['id'])
    if first['status'] != 'completed':
        return
    verdicts = first['result']['results']
    failing = [cid for cid in shorts if verdicts.get(cid, {}).get('status') != 'pass']
    for cid in shorts:
        if cid not in failing:
            compile_for(cid, first)
    if not failing:
        return
    repair = _enqueue(storage, run, 'repair_batch', 'repair', dependencies=[first['id']], volume=True,
                      review_task=first['id'], discover_task=discovery['id'])
    if repair['status'] != 'completed':
        return
    repaired = [cid for cid in failing if cid in repair['result']['repairs']]
    for cid in failing:
        if cid not in repaired:
            compile_for(cid, first)  # No valid repair: hold for the user with the original review.
    if not repaired:
        return
    second = _enqueue(storage, run, 'review_batch', 'round1', dependencies=[repair['id']], volume=True,
                      round=1, discover_task=discovery['id'], repair_task=repair['id'])
    if second['status'] == 'completed':
        for cid in repaired:
            compile_for(cid, second, repair)


def _advance(storage, run):
    if storage.get_collection(run['collection_id'])['status'] == 'cancelled':
        return
    tasks = [task for task in storage.list_tasks(run['collection_id']) if task['payload'].get('run_id') == run['id']]
    for task in tasks:
        if task['status'] == 'held_dependency' and all(storage.get_task(identity)['status'] == 'completed'
                                                        for identity in task['dependencies']):
            storage.retry_task(task['id'])
        retryable = task['error'] in TRANSIENT or (task['error'] in REASK and task['stage'] not in CHECKPOINTED)
        if task['status'] == 'failed' and retryable and task['attempts'] < task['max_attempts']:
            if time.time() < run.get('deadline', run['configuration']['deadline']):
                storage.retry_task(task['id'])
    discovery = next((task for task in tasks if task['stage'] == 'discover'), None)
    if discovery and discovery['status'] == 'completed':
        _advance_batched(storage, run, discovery)
    for discovery_more in tasks:
        if discovery_more['stage'] == 'discover_more' and discovery_more['status'] == 'completed':
            for short in storage.list_shorts(run['collection_id']):
                if short.get('metadata', {}).get('more_task') == discovery_more['id']:
                    _enqueue(storage, run, 'plan_short', ['more', discovery_more['id'], short['id'], 0],
                             short_id=short['id'], dependencies=[discovery_more['id']], round=0,
                             operation='more-' + discovery_more['id'], more_task=discovery_more['id'],
                             constraints=discovery_more['payload']['constraints'], base_revision=None,
                             deadline=discovery_more['payload']['deadline'])
    for task in tasks:
        if task['stage'] == 'plan_short' and task['status'] == 'completed':
            _enqueue(storage, run, 'review_short', task['id'], short_id=task['short_id'],
                     dependencies=[task['id']], plan_task=task['id'], round=task['payload']['round'],
                     base_revision=task['payload'].get('base_revision'),
                     operation=task['payload'].get('operation', 'initial'), **({'more_task': task['payload']['more_task']} if task['payload'].get('more_task') else {}), deadline=task['payload']['deadline'])
        if task['stage'] == 'apply_settings' and task['status'] == 'completed' and (task.get('result') or {}).get('revision_id'):
            _enqueue(storage, run, 'render_preview', task['result']['revision_id'], paid=False,
                     short_id=task['short_id'], dependencies=[task['id']], revision_id=task['result']['revision_id'])
        if task['stage'] == 'review_short' and task['status'] == 'completed':
            review = task.get('result') or {}
            if review.get('status') in {'needs_revision', 'uncertain'} and task['payload']['round'] == 0:
                _enqueue(storage, run, 'plan_short', [task['payload'].get('operation', 'initial'), task['short_id'], 1], short_id=task['short_id'],
                    dependencies=[task['id']], round=1, base_task=task['payload']['plan_task'],
                    base_revision=task['payload'].get('base_revision'),
                    constraints=storage.get_task(task['payload']['plan_task'])['result'].get('constraints', run['configuration']['constraints']),
                    operation=task['payload'].get('operation', 'initial'), **({'more_task': task['payload']['more_task']} if task['payload'].get('more_task') else {}), deadline=task['payload']['deadline'],
                    instruction='Repair these grounded review issues while preserving source meaning: ' + json.dumps(review['issues']))
            if review.get('revision_id'):
                _enqueue(storage, run, 'render_preview', review['revision_id'], paid=False,
                         short_id=task['short_id'], dependencies=[task['id']], revision_id=review['revision_id'])


def drain_generation(storage, sources, collection_id, executor):
    # One graph owner across threads AND processes, including direct batch dispatch.
    root = storage.root / 'collection-coordinators'
    root.mkdir(exist_ok=True)
    with (root / (collection_id + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            from .collection_workflow import schedule_recheck
            schedule_recheck(storage, sources, collection_id, executor)
            return
        return _drain_generation(storage, sources, collection_id, executor)


def _drain_generation(storage, sources, collection_id, executor):
    storage.reconcile_no_request_attempts(collection_id)
    collection = storage.get_collection(collection_id)
    run = storage.get_run(collection['current_run']) if collection.get('current_run') else None
    if not run or collection['status'] == 'cancelled':
        from .collection_workflow import drain_preparation
        return drain_preparation(storage, sources, collection_id, executor)
    # A crash can occur after the run commits but before its complete initial
    # dependency graph commits. Discovery is enqueued last; rebuild that graph
    # idempotently instead of treating a taskless run as completed.
    if run['status'] == 'active' and not any(task['stage'] == 'discover' and
            task['payload'].get('run_id') == run['id'] for task in storage.list_tasks(collection_id)):
        try:
            _seed(storage, sources, run)
        except (ValueError, KeyError, OSError):
            storage.finish_run(run['id'], 'needs_attention')
            telemetry.attributes(generation_outcome='needs_attention')
            return
    pending = {}
    handlers = _handlers(storage, sources, run)
    while True:
        _advance(storage, run)
        tasks = storage.list_tasks(collection_id)
        statuses = {task['id']: task['status'] for task in tasks}
        for lane, pool, stages, limit in (('remote', REMOTE_WORK, set(handlers) - LOCAL_STAGES, 2),
                                         ('local', executor if executor is not None else LOCAL_WORK, LOCAL_STAGES, 1)):
            active = sum(value == lane for value in pending.values())
            ready = sum(t['status'] == 'queued' and t['stage'] in stages and
                        all(statuses.get(dep) == 'completed'
                            for dep in t['dependencies']) for t in tasks)
            for _ in range(min(limit - active, ready)):
                future = pool.submit(copy_context().run, run_one, storage, handlers, collection_id=collection_id, stages=stages)
                pending[future] = lane
        if not pending:
            # Propagate dependency holds/budget decisions before final state.
            if run_one(storage, handlers, collection_id=collection_id) is None:
                break
            continue
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            pending.pop(future)
            future.result()
    _advance(storage, run)
    tasks = [task for task in storage.list_tasks(collection_id) if task['payload'].get('run_id') == run['id']]
    if any(task['status'] in {'queued', 'active'} for task in tasks):
        from .collection_workflow import schedule_recheck
        schedule_recheck(storage, sources, collection_id, executor)
        telemetry.attributes(generation_outcome='pending')
        return
    latest = {}
    for task in tasks:
        if task['short_id']:
            latest[task['short_id']] = task
    unresolved = [task for task in tasks if not task['short_id'] and task['status'] in {'failed', 'held_budget', 'held_dependency', 'cancelled'}]
    unresolved += [task for task in latest.values() if task['status'] in {'failed', 'held_budget', 'held_dependency', 'cancelled'}
                   or (task['stage'] == 'review_short' and (task.get('result') or {}).get('status') in {'needs_revision', 'uncertain'})]
    held = any(short.get('held_revision') for short in storage.list_shorts(collection_id))
    state = 'needs_attention' if unresolved or held else 'completed'
    if storage.get_collection(collection_id)['status'] != 'cancelled' and run['status'] == 'active':
        storage.finish_run(run['id'], state)
    telemetry.attributes(generation_outcome=state)


def improve_short(storage, sources, collection_id, short_id, executor, *, base_revision, instruction,
                  locked_unit_ids=(), request_id, edit_format=None, caption_style=None):
    short = storage.get_short(short_id)
    collection = storage.get_collection(collection_id)
    if short['collection_id'] != collection_id or short['current_revision'] != base_revision:
        raise ValueError('The selected revision changed.')
    if any(t['short_id'] == short_id and t['status'] in {'active', 'queued'} for t in storage.list_tasks(collection_id)):
        raise ValueError('This short is still processing.')
    run = storage.get_run(collection['current_run'])
    plans = [t for t in storage.list_tasks(collection_id) if t['short_id'] == short_id and
             t['stage'] == 'plan_short' and t['status'] == 'completed']
    base = storage.get_revision(base_revision)['document']['plan'] if base_revision else (plans[-1]['result'] if plans else None)
    if base is None:
        raise ValueError('No plan is available to improve.')
    constraints = copy.deepcopy(base.get('constraints', run['configuration']['constraints']))
    constraints['locked_unit_ids'] = list(locked_unit_ids)
    if edit_format is not None:
        if edit_format not in {'auto', 'explanation', 'small_story', 'exchange'}:
            raise ValueError('Unsupported requested edit format.')
        constraints['edit_format'] = edit_format
    if caption_style is not None:
        if caption_style not in {'auto', 'clean_focus', 'editorial', 'phrase'}:
            raise ValueError('Unsupported requested caption style.')
        constraints['caption_style'] = caption_style
    task = _enqueue(storage, run, 'plan_short', [request_id, short_id, 0], short_id=short_id,
                    operation=request_id, round=0, base_revision=base_revision, base_plan=base,
                    instruction=instruction, constraints=constraints, deadline=time.time() + DEFAULT_SECONDS)
    from .collection_workflow import dispatch
    dispatch(storage, sources, collection_id, executor)
    return task


def request_previews(storage, sources, collection_id, executor, short_ids=None):
    """Render server previews on demand; generation itself renders nothing (the studio previews live)."""
    collection = storage.get_collection(collection_id)
    run = storage.get_run(collection['current_run'])
    for short in storage.list_shorts(collection_id):
        revision = short.get('current_revision')
        if revision and not short.get('current_preview') and (short_ids is None or short['id'] in short_ids):
            _enqueue(storage, run, 'render_preview', revision, paid=False, short_id=short['id'], revision_id=revision)
    from .collection_workflow import dispatch
    dispatch(storage, sources, collection_id, executor)


def export_revisions(storage, sources, collection_id, revisions, executor):
    collection = storage.get_collection(collection_id)
    run = storage.get_run(collection['current_run'])
    validated = []
    for selection in revisions:
        short = storage.get_short(selection['short_id'])
        revision = storage.get_revision(selection['revision_id'])
        if short['collection_id'] != collection_id or revision['short_id'] != short['id']:
            raise ValueError('Select revisions from this collection.')
        storage.export_review_state(revision['id'], require=True)
        validated.append((short, revision))
    for short, revision in validated:
        if any(artifact['kind'] == 'export' and artifact['revision_id'] == revision['id']
               for artifact in storage.list_artifacts(short['id'])):
            continue
        identity = [revision['id'], short['restoration_id']] if short.get('restoration_id') else revision['id']
        _enqueue(storage, run, 'render_export', identity, paid=False,
                 short_id=short['id'], revision_id=revision['id'])
    from .collection_workflow import dispatch
    dispatch(storage, sources, collection_id, executor)


def update_settings(storage, sources, collection_id, short_id, executor, *, base_revision, request_id, **settings):
    short = storage.get_short(short_id)
    collection = storage.get_collection(collection_id)
    if short['collection_id'] != collection_id:
        raise ValueError('Short belongs to another collection.')
    identity = key(['settings', short_id, request_id])
    payload = {'base_revision': base_revision, 'settings': settings}
    existing = next((t for t in storage.list_tasks(collection_id)
                     if t['stage'] == 'apply_settings' and t['idempotency_key'] == identity), None)
    if existing:
        if existing['payload'].get('base_revision') != base_revision or existing['payload'].get('settings') != settings:
            raise ValueError('Settings request ID reused with different inputs.')
        task = existing
    else:
        if short['current_revision'] != base_revision:
            raise ValueError('The selected revision changed.')
        if any(t['short_id'] == short_id and t['status'] in {'active', 'queued'} for t in storage.list_tasks(collection_id)):
            raise ValueError('This short is still processing.')
        revision = storage.get_revision(base_revision)
        if revision['short_id'] != short_id:
            raise ValueError('Revision belongs to another short.')
        if revision['document'].get('inspection_only') or revision['document'].get('technical') != 'pass' or revision['document'].get('editorial', {}).get('status') != 'pass':
            raise ValueError('This revision needs editorial and technical review before applying settings.')
        if set(settings.get('shot_framing', {})) - set(revision['document']['plan']['unit_ids']):
            raise ValueError('Shot framing must reference this revision evidence units.')
        run = storage.get_run(collection['current_run'])
        task = storage.enqueue_task(collection_id, 'apply_settings', key([base_revision, settings]), identity,
            short_id=short_id, payload={'run_id': run['id'], **payload})
    from .collection_workflow import dispatch
    dispatch(storage, sources, collection_id, executor)
    return task
