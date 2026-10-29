"""Quality gates before publishing adapters; every external service is fake."""
import copy
from pathlib import Path

import pytest

from publish_support import (connect_youtube, fake_engine, finished_post, publish, studio, video_bytes)  # noqa: F401
from clippie import final_quality, publish_api, studio_api
from clippie.collection_api import store


def test_draft_export_never_prepares_or_downloads_hd_source(studio, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Draft must not fetch masters')
    monkeypatch.setattr(final_quality, 'prepare_sources', forbidden)
    _, short, revision = studio.clip()
    response = studio.client.post(f'/api/studio/clips/{short["id"]}/export',
                                 json={'revision_id': revision['id'], 'quality': 'preview'})
    assert studio.wait(response.json()['job_id'])['status'] == 'done'


def test_final_export_normalizes_render_copy_without_rewriting_revision(studio):
    _, short, revision = studio.clip()
    timeline = studio.detail(short['id'])['timeline']
    timeline['canvas'].update(width=270, height=480)
    saved = studio.client.put(f'/api/studio/clips/{short["id"]}/timeline', json={
        'base_revision': revision['id'], 'timeline': timeline, 'request_id': 'small-canvas'})
    assert saved.status_code == 200, saved.text
    revision = store().get_revision(saved.json()['revision_id'])
    before = copy.deepcopy(revision)
    response = studio.client.post(f'/api/studio/clips/{short["id"]}/export',
                                 json={'revision_id': revision['id'], 'quality': '1080p'})
    job = studio.wait(response.json()['job_id'])
    assert job['status'] == 'done', job
    assert studio.engine.renders[-1]['timeline']['canvas']['width'] == 1080
    assert store().get_revision(revision['id']) == before
    artifact = studio_api.studio().get_export(job['result']['video_url'].split('/')[-2])
    assert artifact['final_quality']['sources']['src']['height'] == 1080
    final_quality.verify_artifact(artifact, short_id=short['id'], revision_id=revision['id'])


@pytest.mark.parametrize('change', ['actual_480p', 'tampered_bytes', 'foreign_revision', 'legacy'])
def test_upload_rechecks_file_and_never_calls_adapter_on_bad_export(publish, change):
    connect_youtube(publish)
    post, short, revision = finished_post(publish)
    artifact = studio_api.studio().get_export(post['artifact_id'])
    if change == 'actual_480p':
        publish.studio.engine.state['media_info'][artifact['video_path']].update(width=640, height=480)
    elif change == 'tampered_bytes':
        Path(artifact['video_path']).write_bytes(b'changed after export')
    else:
        if change == 'foreign_revision':artifact['revision_id'] = 'other-revision'
        else:artifact.pop('final_quality')
        studio_api.studio()._put('exports', artifact)
    publish.service.run_once()
    saved = publish.db.get_post(post['id'])
    assert saved['status'] == 'failed' and saved['error']
    assert publish.google.sessions == 0 and not publish.google.received


def test_legacy_export_is_rerendered_on_explicit_retry_and_old_session_is_cleared(publish):
    connect_youtube(publish)
    post, _, _ = finished_post(publish)
    artifact = studio_api.studio().get_export(post['artifact_id'])
    artifact.pop('final_quality');studio_api.studio()._put('exports', artifact)
    publish.service.run_once()
    assert publish.db.get_post(post['id'])['status'] == 'failed'
    publish.db.update_post(post['id'], upload_session='https://upload.test/old-bytes')
    reply = publish.studio.client.post(f'/api/publish/posts/{post["id"]}/retry')
    assert reply.status_code == 200
    saved = publish.db.get_post(post['id'])
    assert saved['artifact_id'] is None and saved['render_job_id']
    assert saved['upload_session'] is None and publish.google.sessions == 0
    assert publish.studio.wait(saved['render_job_id'])['status'] == 'done'


def test_failed_hd_preparation_can_be_retried_as_a_new_render(publish, monkeypatch):
    connect_youtube(publish)
    original = final_quality.prepare_sources
    def unavailable(*args, **kwargs):
        raise final_quality.FinalQualityError('HD unavailable')
    monkeypatch.setattr(final_quality, 'prepare_sources', unavailable)
    _, short, revision = publish.studio.clip()
    response = publish.studio.client.post('/api/publish/posts', json={
        'short_id': short['id'], 'revision_id': revision['id'], 'platforms': ['youtube'], 'title': 'Test'})
    post = publish.db.get_post(response.json()['posts'][0]['id'])
    assert publish.studio.wait(post['render_job_id'])['status'] == 'failed'
    publish.service.run_once()
    assert publish.db.get_post(post['id'])['status'] == 'failed' and publish.google.sessions == 0
    monkeypatch.setattr(final_quality, 'prepare_sources', original)
    publish.studio.client.post(f'/api/publish/posts/{post["id"]}/retry')
    retried = publish.db.get_post(post['id'])
    assert retried['render_job_id'] != post['render_job_id']
    assert publish.studio.wait(retried['render_job_id'])['status'] == 'done'
