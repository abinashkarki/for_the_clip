"""Clip detail, timeline writes, idempotency, stale revisions and the three actions."""
import json
from pathlib import Path

import pytest

from test_studio_support import fake_engine, no_engine, png_bytes, studio, video_bytes, v2_timeline  # noqa: F401

from clippie import studio_api
from clippie.collection_api import store

EXAMPLE = json.loads((Path(__file__).parents[1] / 'docs' / 'v3' / 'examples' / 'studio_clip.json').read_text())
URL = '/api/studio/clips/{}'


def keys_match(actual, expected, path='$'):
    """Same keys as the contract example, recursing through objects and the first item of non-empty lists."""
    if isinstance(expected, dict) and path.endswith('.sources'):
        assert actual
        keys_match(next(iter(actual.values())), next(iter(expected.values())), path + '.*')
    elif isinstance(expected, dict):
        assert isinstance(actual, dict) and set(actual) == set(expected), path
        for key, item in expected.items():
            if (path != '$.timeline' or key in {'canvas', 'captions'}) and key != 'overrides':
                keys_match(actual[key], item, f'{path}.{key}')
    elif isinstance(expected, list) and expected and actual:
        keys_match(actual[0], expected[0], path + '[]')


def put(studio, clip, timeline, base, request_id='r1'):
    return studio.client.put(URL.format(clip['id']) + '/timeline',
                             json={'base_revision': base, 'timeline': timeline, 'request_id': request_id})


def test_detail_converts_v2_on_read_without_rewriting_the_stored_revision(studio):
    project, short, revision = studio.clip()
    detail = studio.detail(short['id'])
    assert detail['project_id'] == project['id'] and detail['revision_id'] == revision['id']
    assert detail['timeline']['schema_version'] == 3
    assert detail['timeline']['segments'][0]['source_id'] == 'src'
    assert detail['clip']['title'] == 'Khanmigo asks how you would approach it'
    assert detail['clip']['format'] == 'explanation'
    assert detail['clip']['score'] is None and detail['clip']['score_reasons'] == []
    assert detail['clip']['duration'] == 8.0
    assert detail['revisions'] == [{'id': revision['id'], 'created_at': detail['revisions'][0]['created_at'], 'origin': 'generated'}]
    assert detail['revisions'][0]['created_at'].endswith('Z')
    assert store().get_revision(revision['id']) == revision
    assert 'timeline_v3' not in store().get_revision(revision['id'])['document']
    assert len(store().list_revisions(short['id'])) == 1


def test_response_has_the_shape_of_the_contract_example(studio):
    _, short, _ = studio.clip()
    detail = studio.detail(short['id'])
    keys_match(detail, EXAMPLE)
    assert detail['sources']['src'] == {'title': 'Sal Khan on Transforming Education', 'duration': 10.0,
                                        'media_url': '/api/jobs/src/media', 'width': 320, 'height': 180}
    assert detail['clip']['thumbnail_url'] == f'/api/studio/clips/{short["id"]}/thumbnail'
    assert detail['clip']['preview_url'] is None and detail['clip']['status'] == 'ready'


def test_words_carry_program_times_and_removed_words_keep_source_times(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    timeline['segments'][0]['cuts'] = [{'start': 1.0, 'end': 1.4, 'reason': 'filler'}]
    saved = put(studio, short, timeline, revision['id'])
    assert saved.status_code == 200, saved.text
    words = studio.detail(short['id'])['words']
    by_key = {word['key']: word for word in words}
    assert [word['word_id'] for word in words] == list(range(12))
    um = by_key['src:2']
    assert um['removed'] is True and um['program_start'] is None and um['program_end'] is None
    assert (um['start'], um['end']) == (1.0, 1.4)
    kept = by_key['src:3']
    assert kept['removed'] is False and kept['start'] == 1.5 and kept['program_start'] == 1.1
    assert by_key['src:0']['program_start'] == 0.0


def test_unknown_clip_and_unready_clip(studio):
    assert studio.client.get(URL.format('nope')).status_code == 404
    assert studio.client.get(URL.format('../etc')).status_code == 404
    collections = store()
    project = collections.create_collection(['src'])
    short = collections.add_short(project['id'])
    response = studio.client.get(URL.format(short['id']))
    assert response.status_code == 409
    assert studio.client.get(URL.format(short['id']) + '/thumbnail').status_code == 409


def test_put_appends_a_studio_revision_that_keeps_the_base_plan_and_gates(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    timeline['captions']['template_id'] = 'color_word'
    response = put(studio, short, timeline, revision['id'])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['timeline']['captions']['template_id'] == 'color_word'
    saved = store().get_revision(body['revision_id'])
    base = store().get_revision(revision['id'])
    document = saved['document']
    assert document['origin'] == 'studio_edit' and document['timeline_v3'] == body['timeline']
    assert saved['previous_revision'] == revision['id']
    for field in ('plan', 'timeline', 'technical'):
        assert document[field] == base['document'][field]
    assert document['editorial']['status'] == 'pass' and document['editorial']['issues'] == []
    assert store().get_short(short['id'])['current_revision'] == saved['id']
    again = studio.detail(short['id'])
    assert again['timeline']['captions']['template_id'] == 'color_word'
    assert [r['origin'] for r in again['revisions']] == ['studio_edit', 'generated']
    # Old API: gates evaluate as for the base revision, and the short still reads normally.
    assert store().export_review_state(saved['id'])['export_eligible'] is True
    legacy = studio.client.get(f'/api/collections/{short["collection_id"]}/shorts/{short["id"]}')
    assert legacy.status_code == 200 and legacy.json()['current_revision'] == saved['id']


def test_put_with_warnings_in_the_base_review_still_requires_acknowledgement(studio):
    document = {'plan': {'title': 'T', 'timeline': v2_timeline()}, 'timeline': v2_timeline(),
                'editorial': {'status': 'pass', 'issues': [{'severity': 'minor', 'detail': 'x'}]}, 'technical': 'pass'}
    _, short, revision = studio.clip(document=document)
    timeline = studio.detail(short['id'])['timeline']
    new = put(studio, short, timeline, revision['id']).json()['revision_id']
    state = store().export_review_state(new)
    assert state['export_review_required'] and not state['export_eligible']


def test_stale_base_revision_is_409_and_writes_nothing(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    assert put(studio, short, timeline, revision['id'], 'a').status_code == 200
    stale = put(studio, short, timeline, revision['id'], 'b')
    assert stale.status_code == 409 and 'base_revision' in stale.json()['detail']
    unknown = put(studio, short, timeline, 'no-such-revision', 'c')
    assert unknown.status_code == 409
    assert len(store().list_revisions(short['id'])) == 2


def test_put_is_idempotent_on_request_id(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    first = put(studio, short, timeline, revision['id'], 'same')
    replay = put(studio, short, timeline, revision['id'], 'same')
    assert replay.status_code == 200 and replay.json() == first.json()
    assert len(store().list_revisions(short['id'])) == 2
    other = {**timeline, 'audio': {'normalize': False}}
    conflict = put(studio, short, other, revision['id'], 'same')
    assert conflict.status_code == 409 and 'request_id' in conflict.json()['detail']
    assert len(store().list_revisions(short['id'])) == 2


def test_put_rejects_invalid_timelines_with_the_failing_path(studio):
    _, short, revision = studio.clip()
    base = studio.detail(short['id'])['timeline']
    cases = {
        'overlong segment': lambda t: t['segments'][0].update(end=99),
        'cut outside': lambda t: t['segments'][0].update(cuts=[{'start': 0.0, 'end': 1.0, 'reason': 'manual'}]),
        'short cut': lambda t: t['segments'][0].update(cuts=[{'start': 2.0, 'end': 2.01, 'reason': 'manual'}]),
        'unknown source': lambda t: t['segments'][0].update(source_id='other'),
        'bad template': lambda t: t['captions'].update(template_id='nope'),
        'bad canvas': lambda t: t['canvas'].update(width=1000, height=1000, fps=31),
        'no segments': lambda t: t.update(segments=[]),
    }
    for name, mutate in cases.items():
        timeline = json.loads(json.dumps(base))
        mutate(timeline)
        response = put(studio, short, timeline, revision['id'], 'bad-' + name.replace(' ', '-'))
        assert response.status_code == 422, name
        assert isinstance(response.json()['detail'], str) and response.json()['detail'], name
    assert 'segments[0]' in put(studio, short, {**base, 'segments': [{**base['segments'][0], 'end': 99}]},
                                revision['id'], 'path').json()['detail']
    assert len(store().list_revisions(short['id'])) == 1


def test_put_request_body_is_strict(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    url = URL.format(short['id']) + '/timeline'
    body = {'base_revision': revision['id'], 'timeline': timeline, 'request_id': 'x'}
    assert studio.client.put(url, json={**body, 'extra': 1}).status_code == 422
    assert studio.client.put(url, json={**body, 'request_id': ''}).status_code == 422
    assert studio.client.put(url, json={'timeline': timeline}).status_code == 422
    assert studio.client.put(URL.format('missing') + '/timeline', json=body).status_code == 404


def test_cancelled_collection_refuses_edits_with_409(studio):
    project, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    store().cancel_collection(project['id'])
    assert put(studio, short, timeline, revision['id']).status_code == 409


def test_remove_fillers_adds_cuts_and_retimes_shots(studio):
    _, short, revision = studio.clip()
    job = studio.client.post(URL.format(short['id']) + '/actions/auto-reframe',
                             json={'base_revision': revision['id'], 'request_id': 'rf'})
    assert job.status_code == 202
    framed = studio.wait(job.json()['job_id'])
    assert framed['status'] == 'done'
    base = framed['result']['revision_id']
    assert len(framed['result']['timeline']['shots']) == 2
    response = studio.client.post(URL.format(short['id']) + '/actions/remove-fillers',
                                  json={'base_revision': base, 'request_id': 'ff', 'silence_min': 0.6})
    assert response.status_code == 200, response.text
    body = response.json()
    segment = body['timeline']['segments'][0]
    assert segment['cuts'] == [{'start': 1.0, 'end': 1.4, 'reason': 'filler'},
                               {'start': 3.28, 'end': 4.42, 'reason': 'silence'},
                               {'start': 5.0, 'end': 5.4, 'reason': 'filler'}]
    shots = body['timeline']['shots']
    assert shots[0]['start'] == 0.0 and shots[-1]['end'] == 8.0 - 0.4 - 1.14 - 0.4
    assert shots[0]['end'] == shots[1]['start']
    assert body['changed'] is True
    words = studio.detail(short['id'])['words']
    assert [w['word_id'] for w in words if w['removed']] == [2, 7]  # silence gap holds no words
    assert store().get_revision(body['revision_id'])['document']['studio_action'] == 'remove_fillers'


def test_remove_fillers_with_nothing_to_remove_creates_no_revision(studio):
    _, short, revision = studio.clip(timeline=v2_timeline(5.5, 8.0))
    response = studio.client.post(URL.format(short['id']) + '/actions/remove-fillers',
                                  json={'base_revision': revision['id'], 'request_id': 'none'})
    assert response.status_code == 200
    assert response.json()['changed'] is False and response.json()['revision_id'] == revision['id']
    assert len(store().list_revisions(short['id'])) == 1


def test_remove_fillers_trims_a_filler_that_touches_the_segment_edge(studio):
    _, short, revision = studio.clip(timeline=v2_timeline(1.0, 8.0))
    response = studio.client.post(URL.format(short['id']) + '/actions/remove-fillers',
                                  json={'base_revision': revision['id'], 'request_id': 'edge'})
    segment = response.json()['timeline']['segments'][0]
    assert segment['start'] == 1.4
    assert all(cut['start'] > segment['start'] for cut in segment['cuts'])


def test_remove_fillers_keeps_existing_manual_cuts_and_is_idempotent(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    timeline['segments'][0]['cuts'] = [{'start': 6.3, 'end': 6.7, 'reason': 'manual'}]
    base = put(studio, short, timeline, revision['id'], 'manual').json()['revision_id']
    url = URL.format(short['id']) + '/actions/remove-fillers'
    body = {'base_revision': base, 'request_id': 'ff2', 'silence_min': 0.5}
    first = studio.client.post(url, json=body).json()
    assert {'start': 6.3, 'end': 6.7, 'reason': 'manual'} in first['timeline']['segments'][0]['cuts']
    assert studio.client.post(url, json=body).json() == first
    assert studio.client.post(url, json={**body, 'silence_min': 0.9}).status_code == 409
    assert studio.client.post(url, json={**body, 'request_id': 'ff3'}).status_code == 409  # base is now stale
    assert studio.client.post(url, json={**body, 'request_id': 'ff4', 'silence_min': 0}).status_code == 422


def test_auto_reframe_runs_as_a_job_and_writes_shots(studio):
    _, short, revision = studio.clip()
    url = URL.format(short['id']) + '/actions/auto-reframe'
    body = {'base_revision': revision['id'], 'request_id': 'rf1'}
    queued = studio.client.post(url, json=body)
    assert queued.status_code == 202
    job_id = queued.json()['job_id']
    assert studio.client.post(url, json=body).json() == {'job_id': job_id}
    job = studio.wait(job_id)
    assert job['status'] == 'done' and job['progress'] == 1.0 and job['kind'] == 'auto_reframe'
    result = job['result']
    assert [shot['layout'] for shot in result['timeline']['shots']] == ['fill', 'fit']
    assert store().get_short(short['id'])['current_revision'] == result['revision_id']
    assert studio.detail(short['id'])['timeline']['shots'] == result['timeline']['shots']
    assert studio.client.post(url, json={'base_revision': revision['id'], 'request_id': 'rf2'}).status_code == 409


def test_auto_reframe_failure_and_concurrent_edit_fail_the_job_cleanly(studio):
    _, short, revision = studio.clip()
    studio.engine.state['reframe_error'] = 'face model exploded'
    url = URL.format(short['id']) + '/actions/auto-reframe'
    job = studio.wait(studio.client.post(url, json={'base_revision': revision['id'], 'request_id': 'f1'}).json()['job_id'])
    assert job['status'] == 'failed' and 'face model exploded' in job['error'] and job['result'] is None
    assert len(store().list_revisions(short['id'])) == 1
    # A clip edited while the analysis ran: the job must fail instead of overwriting the newer revision.
    studio.engine.state['reframe_error'] = None
    timeline = studio.detail(short['id'])['timeline']
    store().append_revision(short['id'], {**store().get_revision(revision['id'])['document'], 'origin': 'studio_edit'},
                            expected_revision=revision['id'])
    stale = studio_api.studio().create_job('auto_reframe', short['id'])
    with pytest.raises(ValueError, match='changed while'):
        studio_api.run_reframe(studio_api.studio(), stale['id'], short['id'], revision['id'], timeline,
                               {'src': {'id': 'src', 'duration': 10.0, 'words': []}})


def test_apply_brand_kit_writes_template_overlays_and_logo(studio):
    _, short, revision = studio.clip()
    asset = studio.client.post('/api/studio/brand-assets', files={'file': ('logo.png', png_bytes(), 'image/png')}).json()
    kit = studio.client.post('/api/studio/brand-kits', json={
        'name': 'Acme', 'template_id': 'color_word', 'overrides': {'colors': {'active': '#FFE600'}}, 'hook_style': 'hook_outline',
        'progress_bar': {'enabled': True, 'color': '#FFE600'}, 'logo_asset_id': asset['asset_id']}).json()
    url = URL.format(short['id']) + '/actions/apply-brand-kit'
    body = {'base_revision': revision['id'], 'request_id': 'bk', 'brand_kit_id': kit['id']}
    response = studio.client.post(url, json=body)
    assert response.status_code == 200, response.text
    timeline = response.json()['timeline']
    assert timeline['captions']['template_id'] == 'color_word'
    assert timeline['captions']['overrides'] == {'colors': {'active': '#FFE600'}}
    assert timeline['brand_kit_id'] == kit['id']
    kinds = {o['kind']: o for o in timeline['overlays']}
    assert kinds['progress_bar']['color'] == '#FFE600' and kinds['logo']['asset_id'] == asset['asset_id']
    assert studio.client.post(url, json=body).json() == response.json()
    assert studio.client.post(url, json={**body, 'request_id': 'bk2', 'brand_kit_id': 'missing'}).status_code == 404
    assert studio.client.post(url, json={**body, 'request_id': 'bk3', 'base_revision': revision['id']}).status_code == 409


def test_engine_components_missing_give_503(studio, no_engine):
    _, short, revision = studio.clip()
    assert studio.client.get(URL.format(short['id'])).status_code == 503
    assert 'not installed' in studio.client.get(URL.format(short['id'])).json()['detail']
    base = {'base_revision': revision['id'], 'request_id': 'e'}
    assert studio.client.put(URL.format(short['id']) + '/timeline', json={**base, 'timeline': {}}).status_code == 503
    assert studio.client.post(URL.format(short['id']) + '/actions/remove-fillers', json=base).status_code == 503
    assert studio.client.post(URL.format(short['id']) + '/actions/auto-reframe', json=base).status_code == 503
    assert studio.client.post(URL.format(short['id']) + '/export', json={'revision_id': revision['id'], 'quality': 'preview'}).status_code == 503
    assert studio.client.get('/api/studio/templates').status_code == 503
    assert studio.client.get('/api/studio/sources').status_code == 200


def test_a_stored_studio_timeline_is_ignored_once_the_v2_plan_changes_underneath_it(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    timeline['captions']['template_id'] = 'color_word'
    new = put(studio, short, timeline, revision['id']).json()['revision_id']
    document = store().get_revision(new)['document']
    document['timeline'] = v2_timeline(0.0, 7.0)  # what an older-API settings change would do
    document['plan']['timeline'] = document['timeline']
    changed = studio.clip(document=document)[1]
    assert studio.detail(changed['id'])['timeline']['segments'][0]['end'] == 7.0


def test_put_that_only_changes_cuts_retimes_shots_but_never_overrides_client_shot_edits(studio):
    _, short, revision = studio.clip()
    job = studio.wait(studio.client.post(URL.format(short['id']) + '/actions/auto-reframe',
                                         json={'base_revision': revision['id'], 'request_id': 'rf'}).json()['job_id'])
    base = job['result']['revision_id']
    timeline = job['result']['timeline']
    assert [shot['end'] for shot in timeline['shots']] == [4.0, 8.0]
    cut = json.loads(json.dumps(timeline))
    cut['segments'][0]['cuts'] = [{'start': 1.0, 'end': 3.0, 'reason': 'manual'}]
    saved = put(studio, short, cut, base, 'cut')
    assert saved.status_code == 200, saved.text
    shots = saved.json()['timeline']['shots']
    assert shots[0]['start'] == 0.0 and shots[-1]['end'] == 6.0 and shots[0]['end'] == shots[1]['start'] == 2.0
    stale_shots = json.loads(json.dumps(saved.json()['timeline']))
    stale_shots['segments'][0]['cuts'].append({'start': 5.0, 'end': 5.4, 'reason': 'manual'})
    stale_shots['shots'][0]['layout'] = 'fit'  # the client edited shots itself, so the server must not guess
    rejected = put(studio, short, stale_shots, saved.json()['revision_id'], 'cut2')
    assert rejected.status_code == 422 and 'shots' in rejected.json()['detail']
