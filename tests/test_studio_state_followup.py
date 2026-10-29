"""Regressions for clips waiting on shared batch work, using offline state only."""
import pytest

from clippie import studio_api
from test_studio_support import fake_engine, studio, video_bytes  # noqa: F401


def short(number, run='r', **fields):
    return {'id': f's{number}', 'candidate_id': f'c{number}', 'metadata': {'run_id': run}, **fields}


def task(identity, stage, status, run='r', **fields):
    return {'id': identity, 'stage': stage, 'status': status, 'short_id': None,
            'payload': {'run_id': run}, **fields}


def batch_state(status='held_budget'):
    review = task('review', 'review_batch', 'completed', result={'results': {
        f'c{i}': {'status': 'pass' if i < 5 else 'needs_revision'} for i in range(12)}})
    repair = task('repair', 'repair_batch', status,
                  payload={'run_id': 'r', 'review_task': 'review'}, error='budget_exhausted')
    return [review, repair]


def test_original_five_ready_seven_stopped_batch_clips_are_truthful():
    collection = {'status': 'open', 'current_run': 'r'}
    tasks = batch_state()
    clips = [short(i, **({'current_revision': 'revision'} if i < 5 else {})) for i in range(12)]
    states = [studio_api.clip_work_state(s, tasks, collection) for s in clips]
    assert [s['status'] for s in states] == ['ready'] * 5 + ['paused'] * 7
    assert all(s['status_action']['id'] == 'resume_project' for s in states[5:])
    assert all('fixing titles and hooks' in s['status_message'] for s in states[5:])
    # A passing candidate with no compile yet is not dependent on repair.
    assert studio_api.clip_status(short(0), tasks, collection) == 'waiting'


def test_active_batch_and_direct_compile_are_processing_without_stopping_siblings():
    collection = {'status': 'open', 'current_run': 'r'}
    tasks = batch_state('active')
    assert studio_api.clip_status(short(7), tasks, collection) == 'processing'
    assert studio_api.clip_status(short(0), tasks, collection) == 'waiting'
    tasks.append(task('compile', 'compile_short', 'queued', short_id='s0'))
    assert studio_api.clip_status(short(0), tasks, collection) == 'processing'


def test_historical_and_unrelated_more_branches_do_not_block_current_clip():
    collection = {'status': 'open', 'current_run': 'r'}
    old = [task('old-repair', 'repair_batch', 'held_budget', run='old')]
    assert studio_api.clip_status(short(7), old, collection) == 'waiting'
    state = studio_api.clip_work_state(short(7, run='old'), old, collection)
    assert state['status'] == 'paused' and 'earlier generation' in state['status_message']
    assert 'status_action' not in state
    discovery = task('more', 'discover_more', 'completed')
    unrelated = task('more-repair', 'repair_batch', 'held_budget', payload={'run_id': 'r', 'discover_task': 'more'})
    assert studio_api.clip_status(short(7), [discovery, unrelated], collection) == 'waiting'


def test_cancelled_work_and_review_held_clips_remain_distinct():
    cancelled = {'status': 'cancelled', 'current_run': 'r'}
    assert studio_api.clip_status(short(7), batch_state(), cancelled) == 'cancelled'
    assert studio_api.clip_status(short(0, current_revision='rev'), batch_state(), cancelled) == 'ready'
    assert studio_api.clip_status(short(0, held_revision='held'), batch_state(), cancelled) == 'needs_attention'
    tasks = [task('cancel', 'repair_batch', 'cancelled')]
    state = studio_api.clip_work_state(short(7), tasks, {'status': 'open', 'current_run': 'r'})
    assert state['status'] == 'cancelled' and 'status_action' not in state


def test_allowance_message_names_binding_dimension_and_legacy_token_case():
    run = {'id': 'r'}
    held = task('repair', 'repair_batch', 'held_budget', reservation={'tokens': 147456, 'calls': 1})
    collection = {'budget': {'tokens': 933888, 'calls': 16}, 'usage': {'tokens': 880640, 'calls': 10}}
    assert 'more token allowance' in studio_api.project_error([held], run, collection=collection)
    for dimensions, text in [(['calls'], 'request allowance'), (['tokens'], 'token allowance'),
                             (['calls', 'tokens'], 'token and request allowances')]:
        held['budget_block'] = {'dimensions': dimensions}
        assert text in studio_api.project_error([held], run, collection=collection)


def test_progress_identifies_materialized_tasks_instead_of_eventual_work():
    tasks = [task('review', 'review_batch', 'completed'), task('repair', 'repair_batch', 'active')]
    progress = studio_api.project_progress({}, tasks, {'id': 'r'}, 'processing')
    assert progress['basis'] == 'recorded_tasks'
    assert progress['completed_tasks'] == 1 and progress['recorded_tasks'] == 2
    assert progress['label'] == '1 step finished'


@pytest.mark.parametrize('inspection_flag', [True, False])
def test_project_exposes_held_review_read_only_without_enabling_editor_or_export(studio, inspection_flag):
    from clippie.collection_api import store
    document = {'inspection_only': True, 'technical': 'pass',
                'plan': {'title': 'Held title', 'hook_line': 'Life extension'},
                'editorial': {'status': 'needs_revision', 'issues': [
                    {'category': 'faithfulness', 'severity': 'critical', 'detail': 'The hook omits in dish.', 'unit_ids': ['u1']}]}}
    project, short, held = studio.clip(document=document)
    if not inspection_flag:
        # Older held records can lack the inspection flag. Identity still gates export.
        with store()._transaction() as db:
            revision = store()._get(db, held['id'], 'revision')
            revision['document']['inspection_only'] = False
            store()._save(db, 'revision', revision)
    detail = studio.client.get(f'/api/studio/projects/{project["id"]}').json()
    clip = detail['clips'][0]
    assert clip['revision_id'] is None and clip['status'] == 'needs_attention'
    assert clip['held_review']['title'] == 'Held title' and clip['held_review']['hook_line'] == 'Life extension'
    assert clip['held_review']['issues'][0]['detail'] == 'The hook omits in dish.'
    assert detail['clip_counts']['held'] == 1 and detail['task_counts']['running'] == 0
    assert store().get_short(short['id'])['current_revision'] is None
    assert studio.client.get(f'/api/studio/clips/{short["id"]}').status_code == 409
    response = studio.client.post(f'/api/studio/clips/{short["id"]}/export', json={'revision_id': held['id'], 'quality': '1080p'})
    assert response.status_code == 409
    assert 'held for editorial review' in response.json()['detail']
    assert studio.engine.renders == []
    assert studio_api.studio().list_jobs() == []
