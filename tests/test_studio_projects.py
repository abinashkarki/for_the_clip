"""Projects: creation through the existing generation path, listing, detail, and an end-to-end pipeline run."""
import time

import pytest

from test_studio_support import fake_engine, studio, video_bytes  # noqa: F401
from test_collection_generation import pipeline  # noqa: F401

from clippie import app as module, collection_generation
from clippie.collection_api import store

API = '/api/studio'


@pytest.fixture
def started(monkeypatch):
    """Record what project creation asks the generation path to do, without running it."""
    calls = []

    def record(storage, sources, collection_id, executor, **kwargs):
        calls.append({'collection_id': collection_id, **kwargs})
        return {}
    monkeypatch.setattr(collection_generation, 'start_generation', record)
    return calls


def create(studio, **body):
    return studio.client.post(f'{API}/projects', json={'source_ids': ['src'], **body})


def test_create_project_builds_a_collection_and_starts_generation(studio, started):
    response = create(studio, name='My project', options={'aspect': '16:9', 'count': 2, 'max_duration': 45, 'focus': 'Practical advice',
                                                          'template_id': 'bold_pop'}, request_id='req-1')
    assert response.status_code == 201, response.text
    project = response.json()
    assert project['name'] == 'My project' and project['source_ids'] == ['src'] and project['status'] == 'draft'
    assert project['clip_count'] == 0 and project['clips'] == [] and project['created_at'].endswith('Z')
    assert project['sources'][0]['id'] == 'src' and project['progress'] == {'stage': '', 'fraction': 0.0}
    assert project['options'] == {'aspect': '16:9', 'count': 2, 'max_duration': 45.0, 'template_id': 'bold_pop',
                                  'brand_kit_id': None, 'focus': 'Practical advice', 'remove_fillers': True}
    collection = store().get_collection(project['id'])
    assert collection['constraints']['aspect_ratio'] == '16:9' and collection['constraints']['max_shorts'] == 2
    assert collection['constraints']['max_duration'] == 45 and collection['constraints']['focus'] == 'Practical advice'
    assert collection['constraints']['source_roles'] == {'src': 'primary'}
    assert started == [{'collection_id': project['id'], 'request_id': 'req-1'}]
    assert studio.client.get(f'{API}/projects/{project["id"]}').json() == project


def test_count_auto_is_twelve_and_accepts_up_to_thirty(studio, started):
    auto = create(studio).json()
    assert store().get_collection(auto['id'])['constraints']['max_shorts'] == 12
    assert auto['name'] == 'Sal Khan on Transforming Education' and auto['options']['count'] == 'auto'
    big = create(studio, options={'count': 25}).json()
    assert store().get_collection(big['id'])['constraints']['max_shorts'] == 25
    top = create(studio, options={'count': 30}).json()
    assert store().get_collection(top['id'])['constraints']['max_shorts'] == 30
    for bad in (0, 31, -1, True, 'many'):
        assert create(studio, options={'count': bad}).status_code == 422
    assert started[0]['request_id'] == 'initial'


def test_create_project_repeats_with_the_same_request_id(studio, started):
    first = create(studio, request_id='once').json()
    second = create(studio, request_id='once').json()
    assert first['id'] == second['id'] and len(store().list_collections()) == 1
    assert create(studio, request_id='once', name='different').status_code == 409


def test_missing_model_key_is_a_clear_422_and_creates_nothing(studio, started, monkeypatch):
    monkeypatch.setattr(module, 'KEYS', {'gemini': '', 'deepseek': '', 'groq': ''})
    response = create(studio)
    assert response.status_code == 422 and 'Gemini API key' in response.json()['detail']
    assert store().list_collections() == [] and started == []


def test_create_project_validation(studio, started):
    module.STORE.save({'id': 'busy', 'title': 'Busy', 'status': 'transcribing', 'source_path': str(studio.media), 'duration': 5})
    module.STORE.save({'id': 'silent', 'title': 'Silent', 'status': 'ready', 'source_path': str(studio.media), 'duration': 5, 'words': []})
    cases = {
        'no sources': {'source_ids': []},
        'unknown source': {'source_ids': ['ghost']},
        'source not ready': {'source_ids': ['busy']},
        'no speech': {'source_ids': ['silent']},
        'duplicate source': {'source_ids': ['src', 'src']},
        'bad aspect': {'options': {'aspect': '1:1'}},
        'zero count': {'options': {'count': 0}},
        'bool count': {'options': {'count': True}},
        'word count': {'options': {'count': 'many'}},
        'zero duration': {'options': {'max_duration': 0}},
        'long duration': {'options': {'max_duration': 181}},
        'unknown option': {'options': {'tone': 'funny'}},
        'extra field': {'extra': 1},
        'unknown kit': {'options': {'brand_kit_id': 'ghost'}},
        'unknown template': {'options': {'template_id': 'ghost'}},
        'long name': {'name': 'n' * 101},
        'control name': {'name': 'a\x00b'},
    }
    for name, body in cases.items():
        response = studio.client.post(f'{API}/projects', json={'source_ids': ['src'], **body})
        assert response.status_code == 422, (name, response.text)
    assert store().list_collections() == [] and started == []
    assert studio.client.post(f'{API}/projects', json={}).status_code == 422


def test_picture_only_sources_get_the_right_role(studio, started):
    module.STORE.save({'id': 'broll', 'title': 'B-roll', 'status': 'ready', 'source_path': str(studio.media), 'duration': 5, 'words': []})
    project = create(studio, source_ids=['src', 'broll']).json()
    assert store().get_collection(project['id'])['constraints']['source_roles'] == {'src': 'primary', 'broll': 'picture_only'}


def test_a_generation_start_error_is_a_409(studio, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError('Collection already has an active run')
    monkeypatch.setattr(collection_generation, 'start_generation', refuse)
    response = create(studio)
    assert response.status_code == 409 and 'active run' in response.json()['detail']


def test_project_list_and_detail(studio, started):
    assert studio.client.get(f'{API}/projects').json() == []
    older, short, _ = studio.clip()
    newer = create(studio, name='Newer').json()
    listed = studio.client.get(f'{API}/projects').json()
    assert [item['id'] for item in listed] == [newer['id'], older['id']]
    item = next(item for item in listed if item['id'] == older['id'])
    assert set(item) == {'id', 'name', 'source_ids', 'status', 'clip_count', 'created_at', 'thumbnail_url', 'progress', 'lifecycle',
                        'last_activity_at', 'keep_source', 'source_expires_at', 'size_bytes', 'trashed_at', 'purge_at'}
    assert item['status'] == 'ready' and item['clip_count'] == 1 and item['name'] == 'Demo project'
    assert item['thumbnail_url'] == f'{API}/clips/{short["id"]}/thumbnail'
    fresh = next(item for item in listed if item['id'] == newer['id'])
    assert fresh['thumbnail_url'] == f'{API}/sources/src/thumbnail' and fresh['clip_count'] == 0
    detail = studio.client.get(f'{API}/projects/{older["id"]}').json()
    assert detail['progress'] == {'stage': 'Ready', 'fraction': 1.0} and 'error' not in detail
    assert [clip['id'] for clip in detail['clips']] == [short['id']]
    assert set(detail['clips'][0]) == {'id', 'title', 'score', 'score_reasons', 'duration', 'revision_id', 'thumbnail_url',
                                        'preview_url', 'status', 'format', 'post'}
    assert detail['clips'][0]['score'] is None and detail['clips'][0]['duration'] == 8.0
    assert studio.client.get(f'{API}/projects/missing').status_code == 404
    assert studio.client.get(f'{API}/projects/bad!id').status_code == 404


def test_clips_without_scores_keep_creation_order_and_unready_clips_are_summarised(studio):
    project, first, _ = studio.clip()
    collections = store()
    waiting = collections.add_short(project['id'])
    detail = studio.client.get(f'{API}/projects/{project["id"]}').json()
    assert [clip['id'] for clip in detail['clips']] == [first['id'], waiting['id']]
    pending = detail['clips'][1]
    assert pending['revision_id'] is None and pending['thumbnail_url'] is None and pending['duration'] is None
    assert pending['status'] == 'waiting' and detail['clips'][0]['status'] == 'ready'


def test_cancelled_and_failed_projects_report_a_status_and_a_readable_error(studio):
    collections = store()
    cancelled = collections.create_collection(['src'])
    collections.cancel_collection(cancelled['id'])
    assert studio.client.get(f'{API}/projects/{cancelled["id"]}').json()['status'] == 'cancelled'
    failed = collections.create_collection(['src'])
    run = collections.start_run(failed['id'], {'provider': 'gemini'}, 'r1')
    collections.finish_run(run['id'], 'needs_attention')
    detail = studio.client.get(f'{API}/projects/{failed["id"]}').json()
    assert detail['status'] == 'failed' and 'No clips were produced' in detail['error']


def wait_project(client, project_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        project = client.get(f'{API}/projects/{project_id}').json()
        if project['status'] != 'processing':
            return project
        time.sleep(0.05)
    pytest.fail('Project did not finish.')


def test_end_to_end_generation_then_studio_edit_through_the_real_pipeline(pipeline, fake_engine):  # noqa: F811
    client, calls, _ = pipeline
    response = client.post(f'{API}/projects', json={'source_ids': ['s'], 'name': 'Water', 'options': {'count': 3}})
    assert response.status_code == 201, response.text
    project_id = response.json()['id']
    assert response.json()['status'] == 'processing'
    project = wait_project(client, project_id)
    assert project['status'] == 'ready' and project['clip_count'] == 3 and project['progress']['fraction'] == 1.0
    clip = project['clips'][0]
    assert clip['status'] == 'ready' and clip['revision_id'] and clip['duration'] > 0
    scores = [item['score'] for item in project['clips']]
    assert all(isinstance(score, float) for score in scores) and scores == sorted(scores, reverse=True)
    assert clip['title'] == 'Water flow' and clip['format'] == 'explanation' and len(clip['score_reasons']) >= 2
    assert client.get(clip['thumbnail_url']).status_code == 200
    assert clip['preview_url'] is None  # nothing is rendered until export
    detail = client.get(f'{API}/clips/{clip["id"]}').json()
    assert detail['timeline']['schema_version'] == 3 and detail['project_id'] == project_id
    kinds = [overlay['kind'] for overlay in detail['timeline']['overlays']]
    assert kinds == ['hook', 'progress_bar'] and detail['timeline']['captions']['template_id'] == 'bold_pop'
    assert detail['sources']['s']['duration'] == 6.0
    assert [word['removed'] for word in detail['words']] == [False] * len(detail['words'])
    timeline = detail['timeline']
    timeline['captions']['template_id'] = 'color_word'
    before = len(calls)
    saved = client.put(f'{API}/clips/{clip["id"]}/timeline', json={
        'base_revision': detail['revision_id'], 'timeline': timeline, 'request_id': 'e2e'})
    assert saved.status_code == 200, saved.text
    after = client.get(f'{API}/projects/{project_id}').json()
    edited = next(item for item in after['clips'] if item['id'] == clip['id'])
    assert edited['revision_id'] == saved.json()['revision_id'] and edited['preview_url'] is None
    # The older collection API still reads the studio revision and its gates are unchanged.
    legacy = client.get(f'/api/collections/{project_id}/shorts/{clip["id"]}')
    assert legacy.status_code == 200 and legacy.json()['current_revision'] == edited['revision_id']
    assert [stage for stage, _, _ in calls].count('plan') == 0 and len(calls) == before  # studio edits make no model calls


def test_project_options_become_read_time_defaults_without_rewriting_revisions(studio):
    from clippie import studio_api
    project, short, revision = studio.clip()
    assert studio.detail(short['id'])['timeline']['captions']['template_id'] == 'boxed_word'
    studio_api.studio().set_project_options(project['id'], {'template_id': 'phrase_clean'})
    assert studio.detail(short['id'])['timeline']['captions']['template_id'] == 'phrase_clean'
    kit = studio.client.post(f'{API}/brand-kits', json={'name': 'K', 'template_id': 'color_word', 'hook_style': 'hook_outline',
                                                        'progress_bar': {'enabled': True, 'color': '#112233'}}).json()
    studio_api.studio().set_project_options(project['id'], {'template_id': 'phrase_clean', 'brand_kit_id': kit['id']})
    timeline = studio.detail(short['id'])['timeline']
    assert timeline['captions']['template_id'] == 'color_word' and timeline['brand_kit_id'] == kit['id']
    assert timeline['overlays'][0]['color'] == '#112233'
    assert 'timeline_v3' not in store().get_revision(revision['id'])['document']
    assert len(store().list_revisions(short['id'])) == 1
    studio.client.delete(f'{API}/brand-kits/{kit["id"]}')
    assert studio.detail(short['id'])['timeline']['captions']['template_id'] == 'phrase_clean'


def test_transcript_overrun_by_a_hundredth_is_clamped_not_fatal():
    from clippie import source_evidence
    words = [{'id': 0, 'text': 'Hello', 'start': 0.0, 'end': 0.5}, {'id': 1, 'text': "You're", 'start': 9.5, 'end': 10.04}]
    rows = source_evidence._rows(words, 10.0, 'words')
    assert rows[-1]['end'] == 10.0
    bad = [{'id': i, 'text': 'w', 'start': float(i), 'end': float(i) - 0.5} for i in range(1, 20)]
    import pytest
    with pytest.raises(ValueError):
        source_evidence._rows(bad, 30.0, 'words')


def test_a_zero_length_asr_word_is_skipped():
    from clippie import source_evidence
    words = [{'id': 0, 'text': 'a', 'start': 0.0, 'end': 0.5}, {'id': 1, 'text': 'b', 'start': 1.0, 'end': 1.0},
             {'id': 2, 'text': 'c', 'start': 1.2, 'end': 1.6}]
    assert [r['text'] for r in source_evidence._rows(words, 5.0, 'words')] == ['a', 'c']


def test_partial_ready_is_visible_and_free_resume_needs_no_keys_or_allowance(studio, monkeypatch):
    from clippie import collection_workflow
    project, short, _ = studio.clip()
    storage = store()
    run = storage.start_run(project['id'], {}, 'partial')
    # Reproduce a legacy held zero-cost compile without mutating production data.
    pending = storage.enqueue_task(project['id'], 'compile_short', 'v', 'free',
        short_id=storage.add_short(project['id'])['id'], payload={'run_id': run['id']})
    with storage._transaction() as db:
        record = storage._get(db, pending['id'], 'task')
        record.update(status='held_budget', error='budget_exhausted')
        storage._save(db, 'task', record)
    detail = studio.client.get(f'{API}/projects/{project["id"]}').json()
    assert detail['status'] == 'partially_ready' and detail['resumable']
    assert detail['counts']['ready'] == 1 and detail['counts']['stopped'] == 1
    assert detail['progress']['fraction'] < 1
    assert detail['clips'][1]['status'] == 'failed'
    activity = studio.client.get(f'{API}/activity').json()
    assert any(item['id'] == project['id'] and item['state'] == 'attention' for item in activity['items'])
    monkeypatch.setattr(module, 'KEYS', {})
    monkeypatch.setattr(collection_workflow, 'dispatch', lambda *args: None)
    budget = storage.get_collection(project['id'])['budget']
    response = studio.client.post(f'{API}/projects/{project["id"]}/resume')
    assert response.status_code == 202, response.text
    assert storage.get_collection(project['id'])['budget'] == budget
    assert storage.get_task(pending['id'])['status'] == 'queued'
