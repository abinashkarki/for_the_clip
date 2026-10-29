"""Complete synthetic pipeline mechanics, with no model/network quality claim."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

from clippie import app as module, collection_generation as generation
from clippie.collection_api import store
from clippie.store import Store


def judged(**scores):
    return {name: {'score': scores.get(name, 4), 'evidence': f'{name} evidence'}
            for name in ('hook', 'standalone', 'payoff', 'density', 'shareability')}


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    source_path = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30:d=6',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-shortest', str(source_path)], check=True, capture_output=True)
    sources = Store(tmp_path / 'data')
    words = [{'id': i, 'text': text, 'start': i, 'end': i + .8} for i, text in enumerate(
        ['Water.', 'Flows.', 'Storage.', 'Shifts.', 'Markets.', 'Respond.'])]
    sources.save({'id': 's', 'title': 'Synthetic mechanics', 'synthetic': True, 'status': 'ready',
                  'source_path': str(source_path), 'duration': 6., 'words': words})
    monkeypatch.setattr(module, 'STORE', sources)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(module, 'KEYS', {'gemini': 'fake-test-key', 'deepseek': '', 'groq': ''})
    calls = []
    mode = {'bad_review': False, 'fail_plan': None, 'fix_hooks': set(), 'bad_hooks': set()}

    def fake_invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        data = json.loads(prompt.split('\n', 1)[1])
        calls.append((stage, ctx.task['short_id'], data))
        if stage == 'map':
            ids = list(dict.fromkeys(fragment['unit_id'] for fragment in data['packet']['fragments']))
            proposal = {'topics': [{'angle': f'Synthetic topic {i}', 'unit_ids': ids[i:i+2]}
                                  for i in range(0, len(ids), 2)]}
        elif stage == 'discover':
            units = [identity for topic_map in data['topic_maps'] for topic in topic_map['topics'] for identity in topic['unit_ids']]
            hooks = ['Why does this matter so much?', 'What changes after dark?', 'Who reacts first?']
            proposal = {'candidates': [{'id': f'idea{i}', 'title': title, 'angle': title, 'form': 'moment',
                         'edit_format': 'explanation', 'hook_line': hooks[i], 'unit_ids': units[i*2:i*2+2],
                         'components': judged(hook=5 - i)}
                        for i, title in enumerate(['Water flow', 'Evening storage', 'Market response'])],
                        'rejected': [], 'duplicate_groups': [], 'explanation': 'Three synthetic distinct ideas.'}
        elif stage == 'plan':
            if data['candidate']['id'] == mode['fail_plan']:
                raise ValueError('invalid proposal fixture')
            ids = data['candidate']['unit_ids']
            if 'shorter' in data.get('instruction', '').lower():
                ids = list(dict.fromkeys(ids[:1] + data['constraints'].get('locked_unit_ids', [])))
            proposal = {'title': data['candidate']['title'], 'form': 'moment', 'edit_format': data['constraints'].get('edit_format', 'auto') if data['constraints'].get('edit_format', 'auto') != 'auto' else 'explanation', 'format_reason': 'The source explains one supported idea.', 'caption_style': data['constraints'].get('caption_style', 'auto') if data['constraints'].get('caption_style', 'auto') != 'auto' else 'clean_focus', 'caption_reason': 'Readable active-word captions for this synthetic explanation.', 'selections': [{'unit_id': i} for i in ids]}
        elif stage == 'repair':
            proposal = {'repairs': [{'id': c['id'], 'title': c['title'] + ' (fixed)', 'hook_line': 'So what happens next?'}
                                    for c in data['candidates']]}
        elif 'candidates' in data:
            second = any(c['title'].endswith('(fixed)') for c in data['candidates'])
            failing = mode['bad_hooks'] | (set() if second else mode['fix_hooks'])
            proposal = {'results': [{'id': c['id'], 'status': 'needs_revision' if c['id'] in failing else 'pass',
                'issues': ['Hook overstates the speech.'] if c['id'] in failing else []} for c in data['candidates']]}
        else:
            if mode['bad_review']:
                proposal = {'status': 'needs_revision', 'issues': [{'category': 'closure', 'severity': 'critical',
                    'detail': 'Synthetic missing ending.', 'unit_ids': [data['output_order_speech'][-1]['id']]}]}
            else:
                proposal = {'status': 'pass', 'issues': []}
        return {'proposal': proposal, 'usage': {'total_tokens': 20}}

    monkeypatch.setattr(generation.provider, 'invoke', fake_invoke)
    with TestClient(module.app, headers={'X-Clippie-Observation': 'qa'}) as client:
        yield client, calls, mode
    module.WORKER.shutdown(wait=True)


def wait(client, collection_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        document = client.get(f'/api/collections/{collection_id}').json()
        if document.get('run', {}).get('status') != 'active' and not any(
                task['status'] in {'queued', 'active'} for task in document['tasks']):
            return document
        time.sleep(.03)
    pytest.fail('Pipeline did not terminate: ' + json.dumps(document))


def previews(client, collection_id):
    """Generation renders no server preview; render them on demand for tests that need media."""
    wait(client, collection_id)
    generation.request_previews(store(), module.STORE, collection_id, module.WORKER)
    return wait(client, collection_id)


def start(client):
    collection_id = client.post('/api/collections', json={'source_ids': ['s']}).json()['id']
    response = client.post(f'/api/collections/{collection_id}/generate', json={})
    assert response.status_code == 202, response.text
    return collection_id


def test_ranked_clips_without_server_previews_exact_export_and_targeted_improvement(pipeline):
    client, calls, _ = pipeline
    collection_id = start(client)
    ready = wait(client, collection_id)
    assert ready['run']['status'] == 'completed', ready
    assert ready['summary']['ready'] == 3
    assert [stage for stage, _, _ in calls].count('map') == 1
    assert [stage for stage, _, _ in calls].count('discover') == 1
    assert [stage for stage, _, _ in calls].count('review') == 1  # one batched review of every candidate
    assert [stage for stage, _, _ in calls].count('plan') == 0  # candidates compile locally to timeline v3
    assert all(short['preview'] is None for short in ready['shorts'])  # nothing is rendered until export
    ready = previews(client, collection_id)
    before_calls = len(calls)
    assert client.post(f'/api/collections/{collection_id}/generate', json={}).status_code == 202
    assert len(calls) == before_calls
    first, sibling = ready['shorts'][:2]
    video_url = first['preview']['video_url']
    assert client.get(video_url, headers={'Range': 'bytes=0-31'}).status_code == 206
    detail = client.get(f'/api/collections/{collection_id}/shorts/{first["id"]}').json()
    assert len(detail['transcript']) == 2
    review = client.post(f'/api/collections/{collection_id}/shorts/{first["id"]}/decision',
                         json={'revision_id': first['current_revision'], 'rating': 'keep'})
    assert review.status_code == 200
    improved = client.post(f'/api/collections/{collection_id}/shorts/{first["id"]}/improve', json={
        'base_revision': first['current_revision'], 'instruction': 'Shorter',
        'locked_unit_ids': [detail['transcript'][0]['id']]})
    assert improved.status_code == 202, improved.text
    new = wait(client, collection_id)
    updated = next(short for short in new['shorts'] if short['id'] == first['id'])
    other = next(short for short in new['shorts'] if short['id'] == sibling['id'])
    assert updated['current_revision'] != first['current_revision']
    assert updated['decision'] is None
    assert other['current_revision'] == sibling['current_revision']
    assert [stage for stage, _, _ in calls[before_calls:]] == ['plan', 'review']
    assert client.get(video_url).status_code == 200
    export = client.post(f'/api/collections/{collection_id}/exports', json={'revisions': [{
        'short_id': first['id'], 'revision_id': first['current_revision']}]})
    assert export.status_code == 202
    wait(client, collection_id)
    artifacts = store().list_artifacts(first['id'])
    exported = next(item for item in artifacts if item['kind'] == 'export')
    assert exported['revision_id'] == first['current_revision']
    assert exported['document']['profile']['width'] == 1080
    assert len(calls) == before_calls + 2
    # Restoring a base creates a new editing epoch; the same steering request
    # must work again instead of adopting its already completed old task.
    restored = client.post(f'/api/collections/{collection_id}/shorts/{first["id"]}/restore', json={
        'revision_id': first['current_revision'], 'expected_revision': updated['current_revision'],
        'request_id': 'restore-first'})
    assert restored.status_code == 200, restored.text
    assert restored.json()['decision']['rating'] == 'keep'
    assert len(calls) == before_calls + 2
    response = client.post(f'/api/collections/{collection_id}/shorts/{first["id"]}/improve', json={
        'base_revision': first['current_revision'], 'instruction': 'Shorter',
        'locked_unit_ids': [detail['transcript'][0]['id']]})
    assert response.status_code == 202, response.text
    revised = wait(client, collection_id)
    latest = next(short for short in revised['shorts'] if short['id'] == first['id'])
    assert latest['current_revision'] not in {first['current_revision'], updated['current_revision']}
    assert len(calls) == before_calls + 4
    assert client.get(video_url).status_code == 200


def test_restoration_allows_new_export_after_cancelled_historical_attempt(pipeline, monkeypatch):
    client, calls, _ = pipeline
    collection_id = start(client)
    ready = wait(client, collection_id)
    short = ready['shorts'][0]
    from clippie import collection_workflow
    monkeypatch.setattr(collection_workflow._COORDINATORS, 'submit', lambda *args, **kwargs: None)
    monkeypatch.setattr(module.WORKER, 'submit', lambda *args, **kwargs: None)
    body = {'revisions': [{'short_id': short['id'], 'revision_id': short['current_revision']}]}
    assert client.post(f'/api/collections/{collection_id}/exports', json=body).status_code == 202
    failed = store().claim_task(collection_id=collection_id)
    store().finish_task(failed['id'], failed['lease_token'], failed['input_version'], error='render_failed')
    restored = client.post(f'/api/collections/{collection_id}/shorts/{short["id"]}/restore', json={
        'revision_id': short['current_revision'], 'expected_revision': short['current_revision'],
        'request_id': 'restore-after-export-failure'})
    assert restored.status_code == 200, restored.text
    assert store().get_task(failed['id'])['status'] == 'cancelled'
    count = len(calls)
    assert client.post(f'/api/collections/{collection_id}/exports', json=body).status_code == 202
    tasks = [task for task in store().list_tasks(collection_id) if task['stage'] == 'render_export']
    assert len(tasks) == 2
    assert tasks[-1]['status'] == 'queued'
    assert tasks[-1]['payload']['revision_id'] == short['current_revision']
    assert len(calls) == count
    client.post(f'/api/collections/{collection_id}/cancel')


def test_batched_review_repairs_one_hook_and_holds_one(pipeline):
    client, calls, mode = pipeline
    mode['fix_hooks'], mode['bad_hooks'] = {'idea0'}, {'idea1'}
    document = wait(client, start(client))
    assert document['run']['status'] == 'needs_attention'
    assert document['summary']['ready'] == 2 and document['summary']['needs_attention'] == 1
    stages = [stage for stage, _, _ in calls]
    assert stages.count('review') == 2 and stages.count('repair') == 1 and stages.count('plan') == 0
    assert sum(stage in {'map', 'discover', 'review', 'repair'} for stage in stages) == 5
    shorts = {s['candidate_id']: s for s in document['shorts']}
    fixed = shorts['idea0']['revision']['document']
    assert fixed['plan']['title'] == 'Water flow (fixed)' and fixed['editorial']['repaired'] is True
    assert fixed['timeline_v3']['overlays'][0]['text'] == fixed['plan']['hook_line']
    held = shorts['idea1']['held']['revision']['document']
    assert held['inspection_only'] is True and held['editorial']['status'] == 'needs_revision'
    assert shorts['idea1']['revision'] is None and shorts['idea2']['revision']['document']['editorial']['status'] == 'pass'
    assert document['usage']['calls'] == 5


def test_restart_after_completion_adds_no_calls_or_revisions(pipeline):
    client, calls, _ = pipeline
    collection_id = start(client)
    ready = wait(client, collection_id)
    count, revisions = len(calls), [len(store().list_revisions(s['id'])) for s in ready['shorts']]
    for _ in range(2):
        generation.drain_generation(store(), module.STORE, collection_id, module.WORKER)
    again = wait(client, collection_id)
    assert len(calls) == count and again['usage'] == ready['usage']
    assert [len(store().list_revisions(s['id'])) for s in again['shorts']] == revisions


def test_named_combined_source_story_renders_both_primary_recordings(pipeline, monkeypatch, tmp_path):
    client, calls, _ = pipeline
    red = tmp_path / 'red-source.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=30:d=6',
        '-f', 'lavfi', '-i', 'sine=frequency=880:duration=6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(red)], check=True, capture_output=True)
    original = module.STORE.get('s')
    module.STORE.save({**original, 'id': 'second', 'title': 'Synthetic storage', 'source_path': str(red),
        'words': [{**w, 'text': text} for w, text in zip(original['words'], ['Storage.', 'Helps.', 'Evenings.', 'Need.', 'Stored.', 'Energy.'])]})
    module.STORE.save({**original, 'id': 'picture', 'title': 'Synthetic picture only', 'words': []})
    invoke = generation.provider.invoke
    def combine_invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        data = json.loads(prompt.split('\n', 1)[1])
        if stage == 'discover':
            assert {m['source_id'] for m in data['topic_maps']} == {'s', 'second'}
            ids = [m['topics'][0]['unit_ids'][0] for m in data['topic_maps']]
            proposal = {'candidates': [{'id': 'combined', 'title': 'Water and storage', 'angle': 'Combined source story',
                'form': 'story', 'edit_format': 'small_story', 'hook_line': 'What links these two recordings?', 'unit_ids': ids,
                'components': judged()}],
                'rejected': [], 'duplicate_groups': [], 'explanation': 'Two synthetic primary sources.'}
        else:
            return invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs)
        calls.append((stage, ctx.task['short_id'], data))
        return {'proposal': proposal, 'usage': {'total_tokens': 20}}
    monkeypatch.setattr(generation.provider, 'invoke', combine_invoke)
    request = {'source_ids': ['s', 'second', 'picture'], 'name': 'Water & storage', 'request_id': 'combined-once',
        'constraints': {'max_shorts': 1, 'form': 'story', 'source_roles': {'s': 'primary', 'second': 'primary', 'picture': 'picture_only'}}}
    made = client.post('/api/collections', json=request)
    assert made.status_code == 201, made.text
    cid = made.json()['id']
    assert client.post('/api/collections', json=request).json()['id'] == cid
    assert client.post(f'/api/collections/{cid}/generate', json={}).status_code == 202
    document = wait(client, cid)
    assert document['title'] == 'Water & storage'
    assert document['summary']['ready'] == 1, document
    short = document['shorts'][0]
    timeline = short['revision']['document']['timeline']
    assert [clip['source_id'] for clip in timeline['clips']] == ['s', 'second']
    assert 'picture' not in short['revision']['document']['plan']['evidence_provenance']['sources']
    assert [seg['source_id'] for seg in short['revision']['document']['timeline_v3']['segments']] == ['s', 'second']
    short = previews(client, cid)['shorts'][0]
    video = store().get_artifact(short['preview']['id'])['document']['video_path']
    def pixel(at):
        return subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(at), '-i', video,
            '-vf', 'crop=2:2:iw/2:ih/2,scale=1:1,format=rgb24', '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'],
            check=True, capture_output=True).stdout[:3]
    blue, red_pixel = pixel(.4), pixel(1.2)
    assert blue[2] > 180 and blue[0] < 60
    assert red_pixel[0] > 180 and red_pixel[2] < 60
    assert sum(stage == 'map' for stage, _, _ in calls) == 2


def test_visual_samples_are_sent_cached_and_served_without_original_media(pipeline, monkeypatch):
    client, calls, _ = pipeline
    invoke = generation.provider.invoke
    sent = []
    def observe(ctx, api_key, stage, prompt, schema, tokens, **kwargs):
        if stage == 'map':
            sent.extend(kwargs.get('pictures', []))
        return invoke(ctx, api_key, stage, prompt, schema, tokens, **kwargs)
    monkeypatch.setattr(generation.provider, 'invoke', observe)
    cid = start(client); ready = wait(client, cid)
    assert len(sent) == 2 and all(p['source'] == 's' for p in sent)
    groups = ready['visual_evidence']; assert len(groups) == 1
    sample = groups[0]['samples'][0]
    assert sample['sent_to_editor'] is True and sample['id'] in {p['id'] for p in sent}
    image = client.get(sample['image_url'])
    assert image.status_code == 200 and image.headers['content-type'] == 'image/jpeg'
    original = module.STORE.get('s')
    cid2 = start(client); second = wait(client, cid2)
    assert len(sent) == 2  # Derived map and its exact sampled evidence are reused.
    assert second['visual_evidence'][0]['artifact_key'] == groups[0]['artifact_key']
    other = client.post('/api/collections', json={'source_ids': ['s']}).json()['id']
    assert client.get(sample['image_url'].replace(cid, other)).status_code == 404
    from pathlib import Path
    Path(original['source_path']).unlink()
    assert client.get(sample['image_url']).content == image.content


def test_changed_editor_holds_old_run_before_provider_and_requires_new_generation(pipeline, monkeypatch):
    client, calls, _ = pipeline
    submit = module.WORKER.submit
    from clippie import collection_workflow
    coordinator_submit = collection_workflow._COORDINATORS.submit
    monkeypatch.setattr(collection_workflow._COORDINATORS, 'submit', lambda *a, **kw: None)
    monkeypatch.setattr(module.WORKER, 'submit', lambda *a, **kw: None)
    cid = start(client)
    original_version = generation.editor.PROMPT_VERSION
    monkeypatch.setattr(generation.editor, 'PROMPT_VERSION', original_version + 1)
    generation.drain_generation(store(), module.STORE, cid, module.WORKER)
    held = client.get(f'/api/collections/{cid}').json()
    assert not calls and held['run']['status'] == 'needs_attention'
    assert any(t['error'] == 'editor_version_changed' for t in held['tasks'])
    monkeypatch.setattr(module.WORKER, 'submit', submit)
    monkeypatch.setattr(collection_workflow._COORDINATORS, 'submit', coordinator_submit)
    assert client.post(f'/api/collections/{cid}/generate', json={'request_id': 'adopt-version'}).status_code == 202
    ready = wait(client, cid)
    assert ready['summary']['ready'] == 3 and ready['budget'] == held['budget']
    assert store().get_run(ready['run']['id'])['configuration']['prompt_version'] == original_version + 1


def test_picture_only_overlay_renders_picture_without_replacing_speech_audio(pipeline, monkeypatch, tmp_path):
    from array import array
    client, calls, _ = pipeline
    red = tmp_path / 'supporting-red.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=30:d=6',
        '-f', 'lavfi', '-i', 'sine=frequency=880:duration=6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(red)], check=True, capture_output=True)
    module.STORE.save({**module.STORE.get('s'), 'id': 'picture', 'title': 'Supporting red',
                       'source_path': str(red), 'words': []})
    invoke = generation.provider.invoke
    def with_picture(ctx, api_key, stage, prompt, schema, tokens, **kwargs):
        result = invoke(ctx, api_key, stage, prompt, schema, tokens, **kwargs)
        if stage == 'plan':
            pictures = kwargs.get('pictures', [])
            assert pictures and all(p['source'] == 'picture' for p in pictures)
            result['proposal']['supporting_pictures'] = [{'sample_id': pictures[0]['id'],
                'over_unit_id': result['proposal']['selections'][0]['unit_id']}]
        return result
    monkeypatch.setattr(generation.provider, 'invoke', with_picture)
    made = client.post('/api/collections', json={'source_ids': ['s', 'picture'], 'constraints': {
        'max_shorts': 1, 'source_roles': {'s': 'primary', 'picture': 'picture_only'}}})
    cid = made.json()['id']
    assert client.post(f'/api/collections/{cid}/generate', json={}).status_code == 202
    first = wait(client, cid)['shorts'][0]
    assert first['revision']['document']['plan']['visual_evidence'] == []  # generation compiles speech only
    assert client.post(f'/api/collections/{cid}/shorts/{first["id"]}/improve', json={
        'base_revision': first['current_revision'], 'instruction': 'Add a supporting picture'}).status_code == 202
    ready = wait(client, cid); short = ready['shorts'][0]
    assert ready['summary']['ready'] == 1, ready
    plan = short['revision']['document']['plan']
    assert all(c['source_id'] == 's' for c in plan['timeline']['clips'])
    assert plan['timeline']['broll'][0]['source_id'] == 'picture'
    assert plan['visual_retrieval']['mode'] == 'coverage_samples'
    assert plan['visual_evidence'] and 'picture' not in plan['evidence_provenance']['sources']
    path = store().get_artifact(short['preview']['id'])['document']['video_path']
    pixel = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '0.4', '-i', path,
        '-vf', 'crop=2:2:iw/2:ih/2,scale=1:1,format=rgb24', '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'],
        check=True, capture_output=True).stdout[:3]
    assert pixel[0] > 180 and pixel[2] < 60
    pcm = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '0.1', '-i', path, '-t', '0.4',
        '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'], check=True, capture_output=True).stdout
    samples = array('h'); samples.frombytes(pcm)
    frequency = sum(a < 0 <= b for a, b in zip(samples, samples[1:])) / (len(samples) / 16000)
    assert abs(frequency - 440) < 8  # Picture-only 880 Hz audio must never enter the program.
    base = short['current_revision']
    assert client.post(f'/api/collections/{cid}/shorts/{short["id"]}/settings', json={
        'base_revision': base, 'request_id': 'all-framing',
        'framing': {'mode': 'fill', 'zoom': 1.1, 'x': .6, 'y': .5}}).status_code == 202
    edited = wait(client, cid)['shorts'][0]
    assert edited['revision']['document']['timeline']['broll'][0]['framing']['zoom'] == 1.1
    assert client.post(f'/api/collections/{cid}/exports', json={'revisions': [
        {'short_id': short['id'], 'revision_id': base}]}).status_code == 202
    finished = wait(client, cid)
    exports = [a for a in store().list_artifacts(short['id']) if a['kind'] == 'export' and a['revision_id'] == base]
    assert len(exports) == 1, finished
    assert client.get(f'/api/collections/{cid}/artifacts/{exports[0]["id"]}/video',
                      headers={'Range': 'bytes=0-31'}).status_code == 206


def test_more_like_reuses_maps_renders_one_and_explains_duplicate_or_empty(pipeline, monkeypatch):
    client, calls, _ = pipeline
    cid = client.post('/api/collections', json={'source_ids': ['s'], 'constraints': {'max_shorts': 1}}).json()['id']
    assert client.post(f'/api/collections/{cid}/generate', json={}).status_code == 202
    ready = previews(client, cid)
    first = ready['shorts'][0]
    before_revision = first['revision']
    before_video = client.get(first['preview']['video_url']).content
    budget = ready['budget']
    invoke = generation.provider.invoke
    mode = {'value': 'distinct'}
    captured_history = []
    def more_invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        if ctx.task['stage'] == 'discover_more':
            data = json.loads(prompt.split('\n', 1)[1])
            captured_history.append(data['history'])
            ids = list(dict.fromkeys(i for m in data['topic_maps'] for t in m['topics'] for i in t['unit_ids']))
            proposal = {'candidates': [] if mode['value'] == 'empty' else [{
                'id': 'extra-' + mode['value'], 'title': 'Evening storage', 'angle': 'Evening storage',
                'form': 'moment', 'edit_format': 'explanation', 'hook_line': 'What happens in the evening?', 'unit_ids': ids[2:4],
                'components': judged()}],
                'rejected': [], 'duplicate_groups': [], 'explanation': 'Unused source idea.'}
            calls.append(('discover', None, data))
            return {'proposal': proposal, 'usage': {'total_tokens': 20}}
        return invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs)
    monkeypatch.setattr(generation.provider, 'invoke', more_invoke)
    endpoint = f'/api/collections/{cid}/shorts/{first["id"]}/more'
    request = {'base_revision': first['current_revision'], 'request_id': 'one-more'}
    response = client.post(endpoint, json=request)
    assert response.status_code == 202, response.text
    more = wait(client, cid)
    assert more['summary']['ready'] == 2, more
    assert more['budget'] == budget
    assert next(s for s in more['shorts'] if s['id'] == first['id'])['revision'] == before_revision
    assert client.get(first['preview']['video_url']).content == before_video
    assert sum(stage == 'map' for stage, _, _ in calls) == 1
    assert any(h.get('disposition') == 'deferred' for h in captured_history[0])
    count = len(calls)
    assert client.post(endpoint, json=request).status_code == 202
    assert len(calls) == count
    mode['value'] = 'duplicate'
    assert client.post(endpoint, json={**request, 'request_id': 'duplicate'}).status_code == 202
    duplicate = wait(client, cid)
    assert len(duplicate['shorts']) == 2
    assert 'No distinct supported idea' in duplicate['additional_discovery'][-1]['explanation']
    mode['value'] = 'empty'
    assert client.post(endpoint, json={**request, 'request_id': 'empty'}).status_code == 202
    empty = wait(client, cid)
    assert len(empty['shorts']) == 2
    assert empty['budget'] == budget
    assert 'No distinct supported idea' in empty['additional_discovery'][-1]['explanation']


def test_four_source_batch_restart_one_failure_and_independent_previews(pipeline, monkeypatch):
    from clippie import collection_batches as batches, collection_workflow
    from clippie.collection_store import CollectionStore
    client, calls, _ = pipeline
    original_source = module.STORE.get('s')
    for sid in ['s2', 's3', 's4']:
        module.STORE.save({**original_source, 'id': sid, 'title': 'Synthetic ' + sid})
    request = {'request_id': 'four-ready', 'source_ids': ['s', 's2', 's3', 's4'],
               'constraints': {'max_shorts': 1, 'focus': 'Shared default'},
               'overrides': {'s3': {'focus': 'Specific focus'}}}
    made = client.post('/api/collection-batches', json=request)
    assert made.status_code == 201, made.text
    batch = made.json()
    cid_by_source = {m['source_id']: m['collection_id'] for m in batch['members']}
    assert batch['initial_allocated']['calls'] == 64
    assert len(set(cid_by_source.values())) == 4
    assert client.post('/api/collection-batches', json=request).json()['id'] == batch['id']
    original_submit = module.WORKER.submit
    original_coordinator_submit = collection_workflow._COORDINATORS.submit
    monkeypatch.setattr(collection_workflow._COORDINATORS, 'submit', lambda *args, **kwargs: None)
    monkeypatch.setattr(module.WORKER, 'submit', lambda *args, **kwargs: None)
    assert client.post(f'/api/collection-batches/{batch["id"]}/start', json={}).status_code == 202
    assert all(not store().get_collection(cid).get('current_run') for cid in cid_by_source.values())
    invoke = generation.provider.invoke
    def fail_one(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs):
        if ctx.task['collection_id'] == cid_by_source['s2'] and stage == 'review':
            raise ValueError('Synthetic isolated failure')
        return invoke(ctx, api_key, stage, prompt, schema, max_tokens, **kwargs)
    monkeypatch.setattr(generation.provider, 'invoke', fail_one)
    monkeypatch.setattr(module.WORKER, 'submit', original_submit)
    monkeypatch.setattr(collection_workflow._COORDINATORS, 'submit', original_coordinator_submit)
    # Simulate a fresh process: queued in-memory callbacks vanished but the
    # durable batch authorization and children survive in SQLite.
    batches._PENDING.clear()
    reopened = CollectionStore(module.STORE.root)
    batches.resume_batches(reopened.root, module.STORE, module.WORKER)
    collection_workflow.resume_preparation(reopened.root, module.STORE, module.WORKER)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        documents = [client.get('/api/collections/' + cid).json() for cid in cid_by_source.values()]
        if all(d.get('run') and d['run']['status'] != 'active' for d in documents):
            break
        time.sleep(.03)
    assert [d['summary']['ready'] for d in documents] == [1, 0, 1, 1], documents
    assert documents[1]['run']['status'] == 'needs_attention'
    assert [stage for stage, _, _ in calls].count('map') == 4
    assert [stage for stage, _, _ in calls].count('discover') == 4
    assert reopened.get_collection(cid_by_source['s3'])['constraints']['focus'] == 'Specific focus'
    assert all(reopened.get_collection(cid)['source_ids'] == [sid] for sid, cid in cid_by_source.items())
    count = len(calls)
    assert client.post(f'/api/collection-batches/{batch["id"]}/start', json={}).status_code == 202
    assert len(calls) == count
    cancelled = client.post(f'/api/collection-batches/{batch["id"]}/cancel', json={})
    assert cancelled.status_code == 200
    for document in documents:
        for short in document['shorts']:
            if short['preview']:
                assert client.get(short['preview']['video_url']).status_code == 200


def test_recovery_seeds_run_interrupted_before_initial_graph(pipeline):
    from clippie import source_evidence, collection_workflow
    client, calls, _ = pipeline
    cid = client.post('/api/collections', json={'source_ids': ['s'], 'constraints': {'max_shorts': 1}}).json()['id']
    storage = store()
    storage.extend_budget(cid, 16 * generation.provider.RESERVATION_TOKENS, 16, request_id='initial')
    storage.start_run(cid, {'provider': 'gemini', 'source_versions': {'s': source_evidence.evidence_key(module.STORE.get('s'))},
        'constraints': storage.get_collection(cid)['constraints'], 'deadline': time.time() + 600,
        'calls': 16, 'seconds': 600, 'prompt_version': generation.editor.PROMPT_VERSION}, 'interrupted-start')
    assert storage.list_tasks(cid) == []
    collection_workflow.resume_preparation(storage.root, module.STORE, module.WORKER)
    ready = wait(client, cid)
    assert ready['summary']['ready'] == 1
    assert [stage for stage, _, _ in calls].count('map') == 1


def test_visual_settings_render_locally_and_survive_editorial_improvement(pipeline, monkeypatch):
    client, calls, _ = pipeline
    cid = start(client)
    wait(client, cid)
    ready = previews(client, cid)
    first, sibling = ready['shorts'][:2]
    sid = first['id']
    detail = client.get(f'/api/collections/{cid}/shorts/{sid}').json()
    unit = detail['transcript'][0]['id']
    crop = {'mode': 'fill', 'zoom': 1.1, 'x': .8, 'y': .5}
    default = {'mode': 'fill', 'zoom': 1., 'x': .2, 'y': .5}
    before = len(calls)
    usage = ready['usage']
    monkeypatch.setitem(module.KEYS, 'gemini', '')
    result = client.post(f'/api/collections/{cid}/shorts/{sid}/settings', json={
        'base_revision': first['current_revision'], 'request_id': 'crop-and-caption',
        'captions': False, 'caption_style': 'editorial', 'framing': default,
        'shot_framing': {unit: crop}})
    assert result.status_code == 202, result.text
    changed = wait(client, cid)
    short = next(item for item in changed['shorts'] if item['id'] == sid)
    assert short['current_revision'] != first['current_revision']
    assert changed['usage'] == usage and len(calls) == before
    assert next(item for item in changed['shorts'] if item['id'] == sibling['id']) == sibling
    timeline = short['revision']['document']['timeline']
    assert timeline['captions'] is False
    assert [clip['framing'] for clip in timeline['clips']] == [crop, default]
    assert client.get(first['preview']['video_url'], headers={'Range': 'bytes=0-31'}).status_code == 206
    response = client.post(f'/api/collections/{cid}/shorts/{sid}/settings', json={
        'base_revision': short['current_revision'], 'request_id': 'captions-only', 'captions': True})
    assert response.status_code == 202, response.text
    captioned = wait(client, cid)
    short = next(item for item in captioned['shorts'] if item['id'] == sid)
    assert [clip['framing'] for clip in short['revision']['document']['timeline']['clips']] == [crop, default]
    assert len(calls) == before
    monkeypatch.setitem(module.KEYS, 'gemini', 'fake-test-key')
    response = client.post(f'/api/collections/{cid}/shorts/{sid}/improve', json={
        'base_revision': short['current_revision'], 'instruction': 'Add context', 'locked_unit_ids': [unit]})
    assert response.status_code == 202, response.text
    improved = wait(client, cid)
    short = next(item for item in improved['shorts'] if item['id'] == sid)
    plan = short['revision']['document']['plan']
    assert plan['constraints']['shot_framing'][unit] == crop
    assert plan['constraints']['caption_style'] == 'editorial'
    assert plan['constraints']['captions'] is True
    assert plan['timeline']['clips'][plan['unit_ids'].index(unit)]['framing'] == crop
    assert len(calls) == before + 2


def test_new_retry_run_reuses_existing_budget_instead_of_adding_allowance(pipeline, monkeypatch):
    client, _, _ = pipeline
    from clippie import collection_workflow
    monkeypatch.setattr(collection_workflow._COORDINATORS, 'submit', lambda *args, **kwargs: None)
    monkeypatch.setattr(module.WORKER, 'submit', lambda *args, **kwargs: None)
    collection_id = start(client)
    first = client.get(f'/api/collections/{collection_id}').json()
    store().finish_run(first['run']['id'], 'needs_attention')
    retried = client.post(f'/api/collections/{collection_id}/generate',
                          json={'request_id': 'retry-once', 'calls': 16})
    assert retried.status_code == 202
    assert retried.json()['budget'] == first['budget']
    client.post(f'/api/collections/{collection_id}/cancel')


def test_failing_review_has_one_repair_then_inspection_only_hold(pipeline):
    client, calls, mode = pipeline
    mode['bad_hooks'] = {'idea0', 'idea1', 'idea2'}
    collection_id = start(client)
    document = wait(client, collection_id)
    assert document['run']['status'] == 'needs_attention'
    assert all(short['preview'] is None for short in document['shorts'])
    stages = [stage for stage, _, _ in calls]
    assert stages.count('review') == 2 and stages.count('repair') == 1 and stages.count('plan') == 0
    for short in document['shorts']:
        assert short['revision'] is None and short['status'] == 'needs_attention'
        held = short['held']
        revision = held['revision']
        assert revision['document']['inspection_only'] is True
        assert revision['document']['editorial']['status'] == 'needs_revision'
        assert held['preview'] is None
        route = f'/api/collections/{collection_id}/shorts/{short["id"]}'
        detail = client.get(route).json()
        assert len(detail['transcript']) == 2
        for rating in ['revise', 'reject']:
            assert client.post(route + '/decision', json={'revision_id': revision['id'], 'rating': rating}).status_code == 200
        assert client.post(route + '/decision', json={'revision_id': revision['id'], 'rating': 'keep'}).status_code == 409
        assert client.post(route + '/decision', json={'revision_id': revision['id'], 'rating': 'revise', 'acknowledge_warnings': True}).status_code == 409
        assert client.post(f'/api/collections/{collection_id}/exports', json={'revisions': [
            {'short_id': short['id'], 'revision_id': revision['id']}]}).status_code == 409
        assert client.post(route + '/restore', json={'revision_id': revision['id'], 'expected_revision': None,
                                                     'request_id': 'held-restore'}).status_code == 409


def test_budget_exhaustion_holds_without_extra_calls(pipeline):
    client, calls, _ = pipeline
    collection_id = client.post('/api/collections', json={'source_ids': ['s']}).json()['id']
    assert client.post(f'/api/collections/{collection_id}/generate', json={'calls': 1}).status_code == 202
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        document = client.get(f'/api/collections/{collection_id}').json()
        if any(task['status'] == 'held_budget' for task in document['tasks']):
            break
        time.sleep(.02)
    assert len(calls) == 1
    assert document['usage']['calls'] == 1
    assert document['shorts'] == []
    before = len(calls)
    continued = client.post(f'/api/collections/{collection_id}/continue',
                            json={'request_id': 'more-once', 'calls': 8, 'seconds': 600})
    assert continued.status_code == 202, continued.text
    completed = wait(client, collection_id)
    assert completed['summary']['ready'] == 3
    assert len(calls) > before
    budget = completed['budget']
    assert client.post(f'/api/collections/{collection_id}/continue',
                        json={'request_id': 'more-once', 'calls': 8, 'seconds': 600}).status_code == 202
    assert client.get(f'/api/collections/{collection_id}').json()['budget'] == budget


def test_held_improvement_preserves_good_preview_and_sibling_and_passing_successor_clears_hold(pipeline):
    client, calls, mode = pipeline
    collection_id = start(client)
    wait(client, collection_id)
    ready = previews(client, collection_id)
    first, sibling = ready['shorts'][:2]
    route = f'/api/collections/{collection_id}/shorts/{first["id"]}'
    original_revision = first['revision']['id']
    original_preview = first['preview']
    mode['bad_review'] = True
    before = len(calls)
    assert client.post(route + '/improve', json={'base_revision': original_revision,
                                               'instruction': 'More context'}).status_code == 202
    held_collection = wait(client, collection_id)
    held_short = next(s for s in held_collection['shorts'] if s['id'] == first['id'])
    assert held_short['status'] == 'needs_attention'
    assert held_short['revision']['id'] == original_revision and held_short['preview'] == original_preview
    assert next(s for s in held_collection['shorts'] if s['id'] == sibling['id'])['preview'] == sibling['preview']
    assert [stage for stage, _, _ in calls[before:]].count('plan') == 2
    assert [stage for stage, _, _ in calls[before:]].count('review') == 2
    held_id = held_short['held']['revision']['id']
    assert client.post(route + '/decision', json={'revision_id': held_id, 'rating': 'revise'}).status_code == 200
    mode['bad_review'] = False
    assert client.post(route + '/improve', json={'base_revision': original_revision,
                                               'instruction': 'Stronger ending'}).status_code == 202
    successor = next(s for s in wait(client, collection_id)['shorts'] if s['id'] == first['id'])
    assert successor['held'] is None and successor['revision']['id'] != original_revision
    assert client.post(route + '/decision', json={'revision_id': held_id, 'rating': 'reject'}).status_code == 409
    assert client.get(original_preview['video_url'], headers={'Range': 'bytes=0-31'}).status_code == 206
