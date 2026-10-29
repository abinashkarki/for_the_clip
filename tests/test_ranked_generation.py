"""Volume discovery, ranking, batched review and the studio surface, with fake providers and no network."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

from clippie import app as module, clip_scoring, collection_generation as generation
from clippie.collection_api import store
from clippie.store import Store
from test_studio_support import fake_engine  # noqa: F401

UNITS = 52  # 26 two-unit candidates; 30 words per unit keeps the evidence in several packets
API = '/api/studio'


def unit_words(k):
    return [f'Alpha{k}', 'water', 'flows'] + [f'term{k}x{j}' for j in range(26)] + [f'ending{k}.']


@pytest.fixture(scope='module')
def long_video(tmp_path_factory):
    path = tmp_path_factory.mktemp('ranked-media') / 'long.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=2:d=480',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=480', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-shortest', str(path)], check=True, capture_output=True)
    return path


@pytest.fixture
def ranked(tmp_path, monkeypatch, long_video):
    source_path = long_video
    words = []
    for k in range(UNITS):
        for text in unit_words(k):
            at = len(words) * .3
            words.append({'id': len(words), 'text': text, 'start': round(at, 3), 'end': round(at + .25, 3)})
    sources = Store(tmp_path / 'data')
    sources.save({'id': 's', 'title': 'Long interview', 'synthetic': True, 'status': 'ready',
                  'source_path': str(source_path), 'duration': words[-1]['end'] + 1, 'words': words})
    monkeypatch.setattr(module, 'STORE', sources)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(module, 'KEYS', {'gemini': 'fake-test-key', 'deepseek': '', 'groq': ''})
    calls, mode = [], {'fix': set(), 'bad': set(), 'candidates': 25, 'judge': 'same'}

    def components(i):
        return {'hook': {'score': 1 + i % 5, 'evidence': f'Hook note {i}.'},
                'standalone': {'score': 3 + i % 3, 'evidence': 'Self-contained.'},
                'payoff': {'score': 3 + (i * 2) % 3, 'evidence': 'Resolves.'},
                'density': {'score': 1 + (i * 3) % 5, 'evidence': 'Pace note.'},
                'shareability': {'score': 1 + (i * 4) % 5, 'evidence': 'Share note.'}}

    def fake_invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        data = json.loads(prompt.split('\n', 1)[1])
        calls.append((stage, ctx.task['id'], data, kwargs))
        if stage == 'map':
            ids = list(dict.fromkeys(f['unit_id'] for f in data['packet']['fragments']))
            proposal = {'topics': [{'angle': f'Topic {ids[i]}', 'unit_ids': ids[i:i + 4]} for i in range(0, len(ids), 4)]}
        elif stage == 'discover':
            # Discovery receives compact per-window labels, not canonical unit IDs.
            # Derive each supported hook from the transcript sent to the model.
            transcript = data['transcript']
            ids = [row[0] for row in transcript]
            # A question hook: grounded in the spoken name, but not a copy of the opening words.
            hooks = {row[0]: f'Why does {row[2].split()[0]} matter here?' for row in transcript}
            proposal = {'candidates': [{'id': f'idea{i:02d}', 'title': f'Clip {i}', 'angle': f'Angle {i}', 'form': 'moment',
                'edit_format': ['explanation', 'small_story', 'exchange'][i % 3],
                'hook_line': hooks[ids[2 * i]], 'unit_ids': ids[2 * i:2 * i + 2],
                'components': components(i)} for i in range(mode['candidates'])],
                'rejected': [], 'duplicate_groups': [], 'explanation': 'Many distinct ideas.'}
        elif stage == 'repair':
            proposal = {'repairs': [{'id': c['id'], 'title': c['title'] + ' (fixed)', 'hook_line': 'So what really happens next?'}
                                    for c in data['candidates']]}
        elif 'first_words_heard' in data['candidates'][0]:
            # The blind opening judge: 'same' repeats each clip's own hook score, 'inverse' flips it.
            own = {c['id']: 1 + int(c['id'][4:]) % 5 for c in data['candidates']}
            score = {i: (v if mode['judge'] == 'same' else 6 - v) for i, v in own.items()}
            order = sorted(score, key=lambda i: (-score[i], i))
            proposal = {'openings': [{'id': i, 'rank': n + 1, 'score': score[i], 'evidence': 'Opening note.'}
                                     for n, i in enumerate(order)]}
        else:
            second = any(c['title'].endswith('(fixed)') for c in data['candidates'])
            failing = mode['bad'] | (set() if second else mode['fix'])
            proposal = {'results': [{'id': c['id'], 'status': 'needs_revision' if c['id'] in failing else 'pass',
                'issues': ['Hook overstates the speech.'] if c['id'] in failing else []} for c in data['candidates']]}
        return {'proposal': proposal, 'usage': {'total_tokens': 20}}

    monkeypatch.setattr(generation.provider, 'invoke', fake_invoke)
    monkeypatch.setattr('clippie.analyzers.faces.track', lambda *a, **k: (_ for _ in ()).throw(
        __import__('clippie.analyzers', fromlist=['AnalyzerUnavailable']).AnalyzerUnavailable('no model')))
    with TestClient(module.app, headers={'X-Clippie-Observation': 'qa'}) as client:
        yield client, calls, mode
    module.WORKER.shutdown(wait=True)


def wait(client, collection_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        document = client.get(f'/api/collections/{collection_id}').json()
        if document.get('run', {}).get('status') != 'active' and not any(
                task['status'] in {'queued', 'active'} for task in document['tasks']):
            return document
        time.sleep(.05)
    pytest.fail('Pipeline did not terminate: ' + json.dumps(document)[:2000])


def generate(client, **constraints):
    cid = client.post('/api/collections', json={'source_ids': ['s'], 'constraints': constraints}).json()['id']
    assert client.post(f'/api/collections/{cid}/generate', json={}).status_code == 202
    return cid, wait(client, cid)


def expected_scores(count, max_duration=180):
    low, high = clip_scoring.preferred_range(max_duration)
    result = {}
    for i in range(count):
        comps = {'hook': 1 + i % 5, 'standalone': 3 + i % 3, 'payoff': 3 + (i * 2) % 3, 'density': 1 + (i * 3) % 5,
                 'shareability': 1 + (i * 4) % 5}
        result[f'idea{i:02d}'] = clip_scoring.score({n: {'score': v, 'evidence': ''} for n, v in comps.items()}, 2 * 8.95, (low, high))['score']
    return result


def test_twenty_five_candidates_ranked_and_capped_at_the_ceiling(ranked):
    client, calls, _ = ranked
    cid, document = generate(client, max_shorts=25)
    assert document['run']['status'] == 'completed', document['run']
    stages = [stage for stage, *_ in calls]
    assert stages.count('discover') == 1 and stages.count('review') == 1 and 'plan' not in stages and 'repair' not in stages
    discover = next(t for t in store().list_tasks(cid) if t['stage'] == 'discover')
    slate = discover['result']
    assert len(slate['pool']) == 25 and len(slate['candidates']) == 25 - sum(  # weak standalone/payoff are rejected
        1 for c in slate['pool'] if c['disposition'] == 'rejected')
    scores = [c['score'] for c in slate['candidates']]
    assert scores == sorted(scores, reverse=True)
    truth = expected_scores(25)
    assert {c['id']: c['score'] for c in slate['pool']} == truth
    assert all(c['disposition'] == 'selected' for c in slate['pool'] if c['score'] >= min(scores))
    assert document['summary']['ready'] == len(slate['candidates'])


def test_default_ceiling_is_twelve_and_the_best_twelve_win(ranked):
    client, calls, _ = ranked
    cid, document = generate(client)
    discover = next(t for t in store().list_tasks(cid) if t['stage'] == 'discover')['result']
    assert len(discover['candidates']) == 12
    best = sorted(expected_scores(25).items(), key=lambda kv: (-kv[1], kv[0]))
    assert [c['id'] for c in discover['candidates']] == [i for i, _ in best[:12]]
    deferred = {c['id'] for c in discover['pool'] if c['disposition'] == 'deferred'}
    assert deferred == {i for i, _ in best[12:]}
    assert document['summary']['ready'] == 12


def test_hook_lines_titles_formats_and_stored_scores(ranked):
    client, calls, _ = ranked
    cid, document = generate(client, max_shorts=5)
    for short in document['shorts']:
        meta = short['metadata']
        assert set(meta) >= {'score', 'score_reasons', 'score_components', 'score_version', 'hook_line', 'edit_format'}
        assert len(meta['hook_line'].split()) <= 9 and meta['score_version'] == 2 and meta['post_title']
        revision = short['revision']['document']
        assert revision['score']['score'] != 0 and revision['timeline_v3']['overlays'][0]['text'] == meta['hook_line']
        assert revision['plan']['edit_format'] == meta['edit_format']


def test_review_is_one_batched_call_with_every_candidate_and_bounded_speech(ranked):
    client, calls, _ = ranked
    cid, document = generate(client, max_shorts=25)
    reviews = [data for stage, _, data, kwargs in calls if stage == 'review']
    assert len(reviews) == 1
    sent = {c['id'] for c in reviews[0]['candidates']}
    slate = next(t for t in store().list_tasks(cid) if t['stage'] == 'discover')['result']
    assert sent == {c['id'] for c in slate['candidates']}
    assert all(set(c) >= {'title', 'hook_line', 'speech'} for c in reviews[0]['candidates'])
    assert all(kwargs.get('volume') for stage, _, _, kwargs in calls if stage in {'discover', 'review'})


def test_one_failing_hook_is_repaired_once_and_one_is_held(ranked):
    client, calls, mode = ranked
    best = sorted(expected_scores(25).items(), key=lambda kv: (-kv[1], kv[0]))
    fixed, held = best[0][0], best[1][0]
    mode['fix'], mode['bad'] = {fixed}, {held}
    cid, document = generate(client, max_shorts=6)
    stages = [stage for stage, *_ in calls]
    assert stages.count('review') == 2 and stages.count('repair') == 1
    repair = next(data for stage, _, data, _ in calls if stage == 'repair')
    assert {c['id'] for c in repair['candidates']} == {fixed, held}
    second = [data for stage, _, data, _ in calls if stage == 'review'][1]
    assert {c['id'] for c in second['candidates']} == {fixed, held}  # only repaired items are reviewed again
    by_candidate = {s['candidate_id']: s for s in document['shorts']}
    assert by_candidate[fixed]['revision']['document']['plan']['title'] == f'Clip {int(fixed[4:])} (fixed)'
    assert by_candidate[held]['revision'] is None and by_candidate[held]['held']['revision']['document']['inspection_only']
    assert document['run']['status'] == 'needs_attention' and document['summary']['ready'] == 5
    assert document['usage']['calls'] == sum(s in {'map', 'discover', 'review', 'repair'} for s in stages)


def test_budget_accounting_uses_stage_reservations(ranked):
    client, calls, _ = ranked
    cid, document = generate(client, max_shorts=10)
    tasks = store().list_tasks(cid)
    paid = [t for t in tasks if t['reservation']['calls']]
    assert {t['stage'] for t in paid} == {'map_source', 'discover', 'review_batch'}
    volume = generation.provider.VOLUME_RESERVATION_TOKENS
    assert all(t['reservation']['tokens'] == (volume if t['stage'] != 'map_source' else generation.provider.RESERVATION_TOKENS) for t in paid)
    assert all(t['reservation']['calls'] == 0 for t in tasks if t['stage'] == 'compile_short')
    # The fake provider reports no usage, so each paid attempt is charged its full reservation.
    assert document['usage']['calls'] == len(paid)
    assert document['usage']['tokens'] == sum(t['reservation']['tokens'] for t in paid) <= document['budget']['tokens']


def test_restart_does_not_repeat_calls_or_duplicate_revisions(ranked):
    client, calls, _ = ranked
    cid, ready = generate(client, max_shorts=8)
    count = len(calls)
    revisions = {s['id']: len(store().list_revisions(s['id'])) for s in ready['shorts']}
    for _ in range(2):
        generation.drain_generation(store(), module.STORE, cid, module.WORKER)
    assert client.post(f'/api/collections/{cid}/generate', json={}).status_code == 202
    again = wait(client, cid)
    assert len(calls) == count and again['usage'] == ready['usage']
    assert {s['id']: len(store().list_revisions(s['id'])) for s in again['shorts']} == revisions


def test_studio_project_lists_clips_by_score_with_reasons(ranked, fake_engine):  # noqa: F811
    client, calls, _ = ranked
    response = client.post(f'{API}/projects', json={'source_ids': ['s'], 'options': {'count': 25, 'max_duration': 60}})
    assert response.status_code == 201, response.text
    project_id = response.json()['id']
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        project = client.get(f'{API}/projects/{project_id}').json()
        if project['status'] != 'processing':
            break
        time.sleep(.05)
    assert project['status'] == 'ready' and project['options']['count'] == 25
    clips = project['clips']
    assert len(clips) > 12
    scores = [clip['score'] for clip in clips]
    assert all(isinstance(score, float) and 0 <= score <= 100 for score in scores)
    assert scores == sorted(scores, reverse=True)
    top = clips[0]
    assert 2 <= len(top['score_reasons']) <= 4 and top['format'] in {'explanation', 'small_story', 'exchange'}
    assert all(clip['preview_url'] is None and clip['status'] == 'ready' for clip in clips)
    detail = client.get(f'{API}/clips/{top["id"]}').json()
    assert detail['clip']['score'] == top['score'] and detail['timeline']['overlays'][0]['kind'] == 'hook'
    assert client.post(f'{API}/projects', json={'source_ids': ['s'], 'options': {'count': 31}}).status_code == 422


def test_rate_limited_map_pauses_the_project_and_resume_finishes_it(ranked, fake_engine):  # noqa: F811
    client, calls, mode = ranked
    from clippie.collection_provider import CollectionProviderError
    real = generation.provider.invoke
    state = {'failed': 0}

    def flaky(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        if stage == 'map' and state['failed'] < 1:
            state['failed'] += 1
            raise CollectionProviderError('provider_quota')
        return real(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs)
    generation.provider.invoke = flaky
    try:
        project_id = client.post(f'{API}/projects', json={'source_ids': ['s'], 'options': {'count': 5}}).json()['id']
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            project = client.get(f'{API}/projects/{project_id}').json()
            if project['status'] != 'processing':
                break
            time.sleep(.05)
        assert project['status'] == 'failed' and project['resumable'] is True
        assert project['error'].startswith('Paused: the model provider rate-limited')
        resumed = client.post(f'{API}/projects/{project_id}/resume')
        assert resumed.status_code == 202, resumed.text
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            project = client.get(f'{API}/projects/{project_id}').json()
            if project['status'] not in {'processing'}:
                break
            time.sleep(.05)
        assert project['status'] == 'ready' and project['clips'], [(t['stage'], t['status'], t.get('error')) for t in store().list_tasks(project_id)]
        assert client.post(f'{API}/projects/{project_id}/resume').status_code == 409
    finally:
        generation.provider.invoke = real


def test_allowance_scales_with_transcript_packets(ranked):
    client, _, _ = ranked
    from clippie import source_evidence
    project_id = client.post(f'{API}/projects', json={'source_ids': ['s'], 'options': {'count': 5}}).json()['id']
    collection = store().get_collection(project_id)
    packets = len(source_evidence.packets(source_evidence.prepare_evidence(
        module.STORE.get('s'), store().root / 'source-evidence')))
    assert collection['budget']['calls'] >= packets + 6


def _map_wrapper(real, rule):
    """Wrap the fake provider so `rule(call_number, proposal)` can corrupt individual map answers.

    Calls are numbered per project: runs from earlier tests in this module may still be finishing.
    """
    seen = {}

    def invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        response = real(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs)
        if stage == 'map':
            project = ctx.task['collection_id']
            seen[project] = seen.get(project, 0) + 1
            response = {**response, 'proposal': rule(seen[project], response['proposal'])}
        return response
    return invoke, seen


def _settle(client, project_id, timeout=40):
    """Wait until no task is queued or running for several polls: an automatic retry is queued a moment
    after the failure it answers, so a single quiet poll can land in that gap."""
    deadline, quiet = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        busy = any(t['status'] in {'queued', 'active'} for t in store().list_tasks(project_id))
        project = client.get(f'{API}/projects/{project_id}').json()
        quiet = quiet + 1 if not busy and project['status'] != 'processing' else 0
        if quiet >= 6:
            return project
        time.sleep(.05)
    pytest.fail('Project did not settle')


def test_a_mistyped_map_reference_is_dropped_and_the_run_still_makes_clips(ranked, fake_engine):  # noqa: F811
    # Real failure: the model wrote `...0007800` for `...000800`; one bad hint used to stop the whole run.
    client, _, _ = ranked
    real = generation.provider.invoke
    planted = []

    def typo(number, proposal):
        if number != 1:
            return proposal
        first = proposal['topics'][0]
        wrong = first['unit_ids'][0][:-3] + '7' + first['unit_ids'][0][-3:]
        planted.append(wrong)
        return {**proposal, 'topics': [{**first, 'unit_ids': [wrong] + first['unit_ids'][1:]}] + proposal['topics'][1:]}
    generation.provider.invoke, _ = _map_wrapper(real, typo)
    try:
        project_id = client.post(f'{API}/projects', json={'source_ids': ['s'], 'options': {'count': 5}}).json()['id']
        project = _settle(client, project_id)
        assert project['status'] == 'ready' and project['clips']
        maps = [t['result'] for t in store().list_tasks(project_id) if t['stage'] == 'map_source']
        assert sum(m.get('repairs', {}).get('dropped_unit_ids', 0) for m in maps) == 1
        assert planted and planted[0] not in {i for m in maps for t in m['topics'] for i in t['unit_ids']}
    finally:
        generation.provider.invoke = real


def test_an_invalid_map_answer_is_asked_again_once_automatically(ranked, fake_engine):  # noqa: F811
    client, _, _ = ranked
    real = generation.provider.invoke
    generation.provider.invoke, seen = _map_wrapper(real, lambda n, p: {'topics': 'none'} if n == 1 else p)
    try:
        project_id = client.post(f'{API}/projects', json={'source_ids': ['s'], 'options': {'count': 5}}).json()['id']
        project = _settle(client, project_id)
        packets = sum(1 for t in store().list_tasks(project_id) if t['stage'] == 'map_source')
        assert project['status'] == 'ready' and project['clips']
        assert seen[project_id] == packets + 1      # exactly one extra request, for the one bad answer
    finally:
        generation.provider.invoke = real


def test_resume_asks_again_after_a_repeated_validation_failure(ranked, fake_engine):  # noqa: F811
    client, _, _ = ranked
    real = generation.provider.invoke
    state = {'broken': True}
    # Every map answer is invalid until the switch flips, so each map step fails its automatic retry too.
    generation.provider.invoke, _ = _map_wrapper(real, lambda n, p: {'topics': 'none'} if state['broken'] else p)
    try:
        project_id = client.post(f'{API}/projects', json={'source_ids': ['s'], 'options': {'count': 5}}).json()['id']
        project = _settle(client, project_id)
        failed = [t for t in store().list_tasks(project_id) if t['status'] == 'failed']
        assert failed and {t['error'] for t in failed} == {'editor_invalid_output'} and project['resumable'] is True
        assert all(t['attempts'] == 2 for t in failed)            # the automatic re-ask happened once each
        assert 'Resume asks again' in project['error']
        state['broken'] = False
        assert client.post(f'{API}/projects/{project_id}/resume').status_code == 202
        project = _settle(client, project_id)
        assert project['status'] == 'ready' and project['clips']
    finally:
        generation.provider.invoke = real
