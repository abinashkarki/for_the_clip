"""Fail-closed lifecycle behavior while scheduling and deletion lack a shared lock."""
import pytest

from clippie import lifecycle
from test_lifecycle import World, DAY
from test_studio_support import fake_engine, studio, video_bytes  # noqa: F401


def test_sweep_keeps_old_trashed_projects_and_sources(tmp_path):
    world = World(tmp_path / 'data')
    world.source('old-link', age_days=60)
    project = world.project('old-link', created_days_ago=60)
    world.ctx.set_meta(project['id'], trashed_at=world.clock.now - 30 * DAY)
    for dry in (True, False):
        report = lifecycle.sweep(world.ctx, dry_run=dry)
        assert not report['purged_projects'] and not report['freed_sources']
        assert not report['removed_masters'] and report['reclaimable_bytes'] == 0
    assert world.collections.get_collection(project['id'])
    assert (world.root / 'old-link' / 'source.mp4').exists()
    fields = lifecycle.summary_fields(world.ctx, world.collections.get_collection(project['id']))
    assert fields['purge_at'] is None and fields['source_expires_at'] is None


def test_all_destructive_entry_points_fail_closed(tmp_path):
    world = World(tmp_path / 'data')
    world.source('source')
    project = world.project('source')
    world.ctx.set_meta(project['id'], trashed_at=world.clock.now)
    calls = [
        (lifecycle.trash, (world.ctx, project['id'])),
        (lifecycle.free_source, (world.ctx, project['id'])),
        (lifecycle.free_source_files, (world.ctx, 'source')),
        (lifecycle.purge_project, (world.ctx, project['id'])),
        (lifecycle.purge_source, (world.ctx, 'source')),
        (lifecycle.dismiss_source, (world.ctx, 'source')),
        (lifecycle.collect_masters, (world.ctx,)),
    ]
    for func, args in calls:
        with pytest.raises(lifecycle.LifecycleError) as error:
            func(*args)
        assert error.value.status == 503
    assert (world.root / 'source' / 'source.mp4').exists()
    lifecycle.untrash(world.ctx, project['id'])
    assert world.collections.get_collection(project['id'])


def test_destructive_http_routes_report_pause(studio):
    from test_lifecycle_api import API, managed_source, project_on
    managed_source(studio, 'paused-api')
    project, _ = project_on(studio, 'paused-api')
    for method, path in [
        ('post', f'/projects/{project["id"]}/free-source'),
        ('post', f'/projects/{project["id"]}/trash'),
        ('delete', f'/projects/{project["id"]}'),
        ('post', '/sources/paused-api/dismiss'),
        ('post', '/storage/free'),
    ]:
        response = getattr(studio.client, method)(API + path)
        assert response.status_code == 503 and 'temporarily unavailable' in response.json()['detail']
    assert (studio.root / 'paused-api' / 'source.mp4').exists()
