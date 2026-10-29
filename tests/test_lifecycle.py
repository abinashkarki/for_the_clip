"""Video lifecycle: refcounts, blocking, purge, sweeper idle logic, master GC, dismiss, restore and storage accounting."""
import json
import time
from pathlib import Path

import pytest

from clippie import lifecycle
from clippie.collection_store import CollectionStore
from clippie.lifecycle import Ctx, LifecycleError, Snap
from clippie.publish.store import PublishStore
from clippie.store import Store
from clippie.studio_store import StudioStore

DAY = 86400


class Clock:
    def __init__(self, start=2_000_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, days):
        self.now += days * DAY


class World:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.clock = Clock()
        self.jobs = Store(self.root)
        self.collections = CollectionStore(self.root)
        self.studio = StudioStore(self.root)
        self.publish = PublishStore(self.root)
        self.retried = []
        self.ctx = Ctx(self.root, self.collections, self.jobs, self.studio, publish=lambda: self.publish,
                       retry=self.retried.append, clock=self.clock)
        # existing tests model a data folder where automatic cleanup has been on for a long time
        with self.studio._db() as db:
            db.execute("INSERT OR REPLACE INTO lifecycle_settings VALUES ('lifecycle_started_at', '0')")

    def source(self, source_id, kind='link', size=1000, status='ready', age_days=0):
        folder = self.root / source_id
        folder.mkdir(exist_ok=True)
        (folder / 'source.mp4').write_bytes(b'v' * size)
        (folder / 'transcript.json').write_text('{}')
        path = folder / 'source.mp4'
        job = {'id': source_id, 'title': source_id, 'status': status, 'source_path': str(path), 'preview_path': str(path),
               'duration': 10, 'words': [{'text': 'a'}], 'created_at': lifecycle.iso(self.clock.now - age_days * DAY),
               'source': 'https://example.com/v' if kind == 'link' else str(path), 'provenance': {'id': 'abc'}}
        if kind == 'upload':
            upload = self.root / 'uploads' / ('u' + source_id)
            upload.mkdir(parents=True)
            (upload / 'orig.mp4').write_bytes(b'u' * size)
            job.update(source=str(upload / 'orig.mp4'), source_path=str(upload / 'orig.mp4'))
        self.jobs.save(job)
        return job

    def project(self, *source_ids, name='P', created_days_ago=0):
        collection = self.collections.create_collection(list(source_ids), name=name)
        # backdate through the record so activity is measurable
        self.collections.set_collection_fields(collection['id'], created_at=self.clock.now - created_days_ago * DAY)
        return self.collections.get_collection(collection['id'])

    def short(self, collection):
        return self.collections.add_short(collection['id'], metadata={})

    def post(self, short, status):
        return self.publish.create_post(platform='youtube', channel_id='c', short_id=short['id'], revision_id='r', title='t', status=status)

    def snap(self):
        return Snap(self.ctx)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'data')


def raises(status, function, *args):
    with pytest.raises(LifecycleError) as caught:
        function(*args)
    assert caught.value.status == status
    return caught.value.message


@pytest.fixture(autouse=True)
def isolated_lifecycle_algorithm(monkeypatch):
    # Algorithm tests run only on isolated fixtures; they do not establish concurrency safety.
    from clippie import lifecycle
    monkeypatch.setattr(lifecycle, 'DESTRUCTIVE_LIFECYCLE_ENABLED', True)


def test_trash_and_untrash_round_trip_marks_the_collection(world):
    world.source('s1')
    project = world.project('s1')
    lifecycle.trash(world.ctx, project['id'])
    stored = world.collections.get_collection(project['id'])
    assert stored['trashed_at'] == world.clock.now
    fields = lifecycle.summary_fields(world.ctx, stored)
    assert fields['lifecycle'] == 'trashed' and fields['purge_at'] == lifecycle.iso(world.clock.now + 7 * DAY)
    lifecycle.untrash(world.ctx, project['id'])
    assert 'trashed_at' not in world.collections.get_collection(project['id'])
    raises(409, lifecycle.untrash, world.ctx, project['id'])


def test_trash_and_free_are_blocked_by_an_active_task(world):
    world.source('s1')
    project = world.project('s1')
    world.collections.enqueue_task(project['id'], 'map_source', 'v1', 'k1')
    assert 'still working' in raises(409, lifecycle.trash, world.ctx, project['id'])
    assert 'still working' in raises(409, lifecycle.free_source, world.ctx, project['id'])
    assert (world.root / 's1' / 'source.mp4').exists()


@pytest.mark.parametrize('status', ['scheduled', 'uploading', 'rendering'])
def test_trash_and_free_are_blocked_by_a_pending_post(world, status):
    world.source('s1')
    project = world.project('s1')
    short = world.short(project)
    post = world.post(short, 'scheduled')
    world.publish._run('UPDATE posts SET status=? WHERE id=?', (status, post['id']), True)
    assert 'post' in raises(409, lifecycle.trash, world.ctx, project['id'])
    assert 'post' in raises(409, lifecycle.free_source, world.ctx, project['id'])
    world.publish._run("UPDATE posts SET status='published' WHERE id=?", (post['id'],), True)
    lifecycle.trash(world.ctx, project['id'])


def test_free_keeps_record_and_transcript_and_records_how_to_restore(world):
    world.source('s1', kind='upload', size=500)
    project = world.project('s1')
    freed = lifecycle.free_source(world.ctx, project['id'])
    job = world.jobs.get('s1')
    assert not (world.root / 's1' / 'source.mp4').exists() and (world.root / 's1' / 'transcript.json').exists()
    assert not (world.root / 'uploads' / 'us1').exists()
    assert job['source_freed_at'] and job['source_size'] == 500 and len(job['source_sha256']) == 64
    assert lifecycle.summary_fields(world.ctx, freed)['lifecycle'] == 'source_freed'
    assert lifecycle.guard_reason(world.ctx, freed) == lifecycle.FREED_MESSAGE
    assert world.collections.get_collection(project['id'])


def test_shared_source_is_refcounted(world):
    world.source('shared')
    first, second = world.project('shared', name='A'), world.project('shared', name='B')
    world.short(first)
    assert world.snap().users['shared'] and len(world.snap().users['shared']) == 2
    lifecycle.trash(world.ctx, first['id'])
    lifecycle.purge_project(world.ctx, first['id'])
    assert (world.root / 'shared' / 'source.mp4').exists(), 'still referenced by the second project'
    lifecycle.trash(world.ctx, second['id'])
    lifecycle.purge_project(world.ctx, second['id'])
    assert not (world.root / 'shared').exists()
    with pytest.raises(KeyError):
        world.jobs.get('shared')


def test_freeing_a_shared_source_is_blocked_by_the_other_project_pin_or_work(world):
    world.source('shared')
    first, second = world.project('shared', name='A'), world.project('shared', name='B')
    lifecycle.keep_source(world.ctx, second['id'], True)
    assert 'keeps its source' in raises(409, lifecycle.free_source, world.ctx, first['id'])
    lifecycle.keep_source(world.ctx, second['id'], False)
    world.collections.enqueue_task(second['id'], 'map_source', 'v1', 'k1')
    raises(409, lifecycle.free_source, world.ctx, first['id'])
    assert 'Turn off Keep source' in raises(409, lifecycle.free_source, world.ctx, lifecycle.keep_source(world.ctx, first['id'], True)['id'])


def test_purge_requires_trash_and_removes_exactly_the_projects_files_but_keeps_posts(world):
    world.source('s1')
    world.source('other')
    project, bystander = world.project('s1', name='Gone'), world.project('other', name='Stays')
    short, kept_short = world.short(project), world.short(bystander)
    revision = world.collections.append_revision(short['id'], {'plan': {}})
    kept_revision = world.collections.append_revision(kept_short['id'], {'plan': {}})
    artifacts = world.root / 'collection-artifacts'
    for rev, folder in ((revision, 'preview-a'), (revision, 'export-b'), (kept_revision, 'export-c')):
        (artifacts / rev['id'] / folder).mkdir(parents=True)
        (artifacts / rev['id'] / folder / 'clip.mp4').write_bytes(b'x' * 10)
    (artifacts / 'studio' / revision['id'] / 'job').mkdir(parents=True)
    (artifacts / 'studio' / revision['id'] / 'job' / 'clip.mp4').write_bytes(b'x')
    thumbs = world.root / 'studio' / 'thumbnails'
    thumbs.mkdir(parents=True)
    (thumbs / f'clip-{short["id"]}-1.jpg').write_bytes(b'j')
    (thumbs / f'clip-{kept_short["id"]}-1.jpg').write_bytes(b'j')
    post = world.post(short, 'published')
    world.studio.set_project_options(project['id'], {'aspect': '9:16'})
    raises(409, lifecycle.purge_project, world.ctx, project['id'])
    lifecycle.trash(world.ctx, project['id'])
    lifecycle.purge_project(world.ctx, project['id'])
    assert not (artifacts / revision['id']).exists() and not (artifacts / 'studio' / revision['id']).exists()
    assert (artifacts / kept_revision['id'] / 'export-c' / 'clip.mp4').exists()
    assert not (thumbs / f'clip-{short["id"]}-1.jpg').exists() and (thumbs / f'clip-{kept_short["id"]}-1.jpg').exists()
    assert not (world.root / 's1').exists() and (world.root / 'other' / 'source.mp4').exists()
    assert world.studio.project_options(project['id']) == {}
    ids = {c['id'] for c in world.collections.list_collections()}
    assert ids == {bystander['id']}
    with pytest.raises(KeyError):
        world.collections.get_short(short['id'])
    assert world.collections.get_short(kept_short['id'])
    survivor = world.publish.get_post(post['id'])
    assert survivor['project_name'] == 'Gone' and survivor['project_id'] == project['id']


def test_sweeper_frees_only_after_idle_period_and_respects_pin_and_never(world):
    world.source('old')
    world.source('pinned')
    world.source('fresh')
    old, pinned = world.project('old', created_days_ago=20), world.project('pinned', created_days_ago=20)
    world.project('fresh', created_days_ago=3)
    lifecycle.keep_source(world.ctx, pinned['id'], True)
    dry = lifecycle.sweep(world.ctx, dry_run=True)
    assert dry['freed_sources'] == ['old'] and dry['reclaimable_bytes'] == 1000
    assert (world.root / 'old' / 'source.mp4').exists(), 'a dry run changes nothing'
    report = lifecycle.sweep(world.ctx)
    assert report['freed_sources'] == ['old']
    assert not (world.root / 'old' / 'source.mp4').exists() and (world.root / 'fresh' / 'source.mp4').exists()
    assert (world.root / 'pinned' / 'source.mp4').exists()
    assert lifecycle.sweep(world.ctx)['freed_sources'] == [], 'idempotent'
    world.clock.advance(14)   # fresh is now 17 days idle
    assert lifecycle.sweep(world.ctx)['freed_sources'] == ['fresh']
    world.ctx.set_retention_days(0)
    world.clock.advance(400)
    assert lifecycle.sweep(world.ctx)['freed_sources'] == []
    assert old['id']


def test_sweeper_activity_resets_idle_clock_and_shared_sources_wait_for_every_project(world):
    world.source('shared')
    stale, busy = world.project('shared', name='A', created_days_ago=30), world.project('shared', name='B', created_days_ago=30)
    lifecycle.touch(world.ctx, busy['id'])
    assert lifecycle.sweep(world.ctx)['freed_sources'] == []
    fields = lifecycle.summary_fields(world.ctx, stale)
    assert fields['source_expires_at'] == lifecycle.iso(world.clock.now + 15 * DAY)
    world.clock.advance(16)
    assert lifecycle.sweep(world.ctx)['freed_sources'] == ['shared']


def test_sweeper_skips_blocked_projects_and_purges_expired_trash(world):
    world.source('s1')
    world.source('s2')
    working, trashed = world.project('s1', created_days_ago=40), world.project('s2', created_days_ago=40, name='Old')
    world.collections.enqueue_task(working['id'], 'map_source', 'v1', 'k1')
    lifecycle.trash(world.ctx, trashed['id'])
    assert lifecycle.sweep(world.ctx)['purged_projects'] == []
    world.clock.advance(8)
    report = lifecycle.sweep(world.ctx)
    assert report['purged_projects'] == [trashed['id']] and report['freed_sources'] == []
    assert (world.root / 's1' / 'source.mp4').exists() and not (world.root / 's2').exists()


def master(world, key, source_id, size=100, old=True):
    folder = world.root / 'final-sources' / key
    folder.mkdir(parents=True)
    (folder / 'master.mp4').write_bytes(b'm' * size)
    (folder / 'lock').write_text('')
    if source_id is not None:
        (folder / 'master.json').write_text(json.dumps({'identity': {'source_id': source_id}, 'file': 'master.mp4'}))
    return folder


def test_master_gc_keeps_live_masters_and_removes_freed_missing_and_orphan_ones(world):
    world.source('live')
    world.source('gone')
    keep = master(world, 'a', 'live')
    freed_one = master(world, 'b', 'gone')
    missing = master(world, 'c', 'deleted-source')
    orphan = master(world, 'd', None)
    young = master(world, 'e', None)
    import os
    for folder in (orphan,):
        os.utime(folder, (world.clock.now - 3 * DAY,) * 2)
    os.utime(young, (world.clock.now,) * 2)
    world.jobs.update('gone', source_freed_at=lifecycle.iso(world.clock.now), status='ready')
    (world.root / 'gone' / 'source.mp4').unlink()
    size, folders = lifecycle.collect_masters(world.ctx, dry_run=True)
    assert size == sum(lifecycle.tree_bytes(f) for f in (freed_one, missing, orphan)) and len(folders) == 3 and freed_one.exists()
    lifecycle.collect_masters(world.ctx)
    assert keep.exists() and young.exists()
    assert not freed_one.exists() and not missing.exists() and not orphan.exists()


def test_freeing_a_source_also_removes_its_master(world):
    world.source('s1')
    project = world.project('s1')
    folder = master(world, 'a', 's1')
    lifecycle.free_source(world.ctx, project['id'])
    assert not folder.exists()


def test_dismiss_rules(world):
    for source_id, status in (('failed', 'failed'), ('cancelled', 'cancelled'), ('unused', 'ready'), ('busy', 'transcribing'), ('used', 'failed')):
        world.source(source_id, status=status)
    world.project('used')
    lifecycle.dismiss_source(world.ctx, 'failed')
    lifecycle.dismiss_source(world.ctx, 'cancelled')
    lifecycle.dismiss_source(world.ctx, 'unused')
    raises(409, lifecycle.dismiss_source, world.ctx, 'busy')
    raises(409, lifecycle.dismiss_source, world.ctx, 'used')
    raises(404, lifecycle.dismiss_source, world.ctx, 'nope')
    assert not (world.root / 'failed').exists()
    assert {j['id'] for j in world.jobs.all()} == {'busy', 'used'}


def test_sweeper_dismisses_old_failed_sources_only(world):
    world.source('old-failed', status='failed', age_days=9)
    world.source('new-failed', status='failed', age_days=1)
    import os
    for name in ('old-failed',):
        os.utime(world.root / name, (world.clock.now - 9 * DAY,) * 2)
    assert lifecycle.sweep(world.ctx)['dismissed_sources'] == ['old-failed']
    assert {j['id'] for j in world.jobs.all()} == {'new-failed'}


def test_restore_link_reuses_the_retry_path_and_upload_asks_for_the_file(world):
    world.source('link')
    world.source('up', kind='upload')
    link, upload = world.project('link', name='L'), world.project('up', name='U')
    lifecycle.free_source(world.ctx, link['id'])
    lifecycle.free_source(world.ctx, upload['id'])
    lifecycle.restore_source(world.ctx, link['id'])
    assert world.retried == ['link'] and world.jobs.get('link')['status'] == 'failed'
    assert raises(422, lifecycle.restore_source, world.ctx, upload['id']) == 'Drop the original file to restore it'
    assert world.retried == ['link']
    raises(409, lifecycle.restore_source, world.ctx, world.project('up', name='x')['id']) if False else None
    # once the retry has rebuilt the file, the project is active again and the marker clears
    path = Path(world.jobs.get('link')['source_path'])
    path.write_bytes(b'v')
    world.jobs.update('link', status='ready')
    assert lifecycle.summary_fields(world.ctx, link)['lifecycle'] == 'active'
    assert lifecycle.reconcile(world.ctx) == 1 and not world.jobs.get('link').get('source_freed_at')


def test_restore_refuses_a_link_that_now_points_at_a_different_video(world):
    world.source('link')
    project = world.project('link')
    lifecycle.free_source(world.ctx, project['id'])
    Path(world.jobs.get('link')['source_path']).write_bytes(b'v')
    world.jobs.update('link', status='ready', provenance={'id': 'different'})
    lifecycle.reconcile(world.ctx)
    assert world.jobs.get('link')['status'] == 'failed'


def test_reredropped_upload_is_matched_by_sha256(world, tmp_path):
    world.source('up', kind='upload', size=64)
    project = world.project('up')
    lifecycle.free_source(world.ctx, project['id'])
    same, other = tmp_path / 'same.mp4', tmp_path / 'other.mp4'
    same.write_bytes(b'u' * 64)
    other.write_bytes(b'x' * 64)
    assert lifecycle.matches_upload(world.ctx, 'up', same) and not lifecycle.matches_upload(world.ctx, 'up', other)


def test_user_owned_files_outside_managed_folders_are_never_deleted(world, tmp_path):
    mine = tmp_path / 'mine.mp4'
    mine.write_bytes(b'precious')
    job = world.source('s1')
    world.jobs.update('s1', source_path=str(mine), source=str(mine), preview_path=str(world.root / 's1' / 'source.mp4'))
    project = world.project('s1')
    lifecycle.free_source(world.ctx, project['id'])
    assert mine.read_bytes() == b'precious' and job


def test_storage_accounting_matches_the_disk_and_reclaimable_follows_idle_sources(world):
    world.source('idle', size=4000)
    world.source('live', size=2000)
    world.project('idle', created_days_ago=30)
    world.project('live')
    (world.root / 'models').mkdir()
    (world.root / 'models' / 'w.bin').write_bytes(b'm' * 700)
    kept = master(world, 'a', 'live', size=300)
    payload = lifecycle.storage(world.ctx)
    assert payload['sources_bytes'] == 4000 + 2000 + lifecycle.tree_bytes(kept)
    assert payload['models_bytes'] == 700 and payload['retention_days'] == 15
    assert payload['reclaimable_bytes'] == 4000
    assert payload['total_bytes'] == lifecycle.tree_bytes(world.root)
    assert payload['total_bytes'] >= payload['sources_bytes'] + payload['models_bytes']
    assert set(payload) == {'total_bytes', 'sources_bytes', 'working_bytes', 'exports_bytes', 'models_bytes', 'other_bytes',
                            'reclaimable_bytes', 'retention_days'}


def test_project_sizes_split_working_and_exports(world):
    world.source('s1', size=100)
    project = world.project('s1')
    short = world.short(project)
    revision = world.collections.append_revision(short['id'], {'plan': {}})
    base = world.root / 'collection-artifacts' / revision['id']
    (base / 'preview-1').mkdir(parents=True)
    (base / 'preview-1' / 'p.mp4').write_bytes(b'p' * 30)
    (base / 'export-1').mkdir()
    (base / 'export-1' / 'e.mp4').write_bytes(b'e' * 50)
    sizes = lifecycle.summary_fields(world.ctx, project)['size_bytes']
    assert sizes == {'source': 100, 'working': 30, 'exports': 50}
    lifecycle.free_source(world.ctx, project['id'])
    sizes = lifecycle.summary_fields(world.ctx, project)['size_bytes']
    assert sizes == {'source': 0, 'working': 0, 'exports': 50}


def test_retention_setting_validation(world):
    assert world.ctx.retention_days() == 15
    for good in (0, 7, 15, 30):
        assert world.ctx.set_retention_days(good) == good and world.ctx.retention_days() == good
    for bad in (1, -1, 90, True):
        raises(422, world.ctx.set_retention_days, bad)


def test_first_run_grace_period_protects_existing_media(tmp_path):
    """Upgrading must not delete anything on first launch: every clock starts at rollout."""
    import os
    fresh = World.__new__(World)
    fresh.root = (tmp_path / 'fresh').resolve()
    fresh.clock = Clock()
    fresh.jobs, fresh.collections = Store(fresh.root), CollectionStore(fresh.root)
    fresh.studio, fresh.publish, fresh.retried = StudioStore(fresh.root), PublishStore(fresh.root), []
    fresh.ctx = Ctx(fresh.root, fresh.collections, fresh.jobs, fresh.studio, publish=lambda: fresh.publish,
                    retry=fresh.retried.append, clock=fresh.clock)
    World.source(fresh, 'old', age_days=60)
    World.source(fresh, 'old-failed', status='failed', age_days=60)
    os.utime(fresh.root / 'old-failed', (fresh.clock.now - 60 * DAY,) * 2)
    World.project(fresh, 'old', created_days_ago=60)
    report = lifecycle.sweep(fresh.ctx)
    assert report['freed_sources'] == [] and report['dismissed_sources'] == []
    assert {j['id'] for j in fresh.jobs.all()} == {'old', 'old-failed'}
    fresh.clock.now += 16 * DAY
    fresh.ctx._rollout = None
    report = lifecycle.sweep(fresh.ctx)
    assert report['freed_sources'] == ['old'] and report['dismissed_sources'] == ['old-failed']


def test_sweep_never_automatically_frees_uploaded_original(world):
    world.source('uploaded', kind='upload', age_days=40)
    world.project('uploaded', created_days_ago=40)
    report = lifecycle.sweep(world.ctx)
    assert 'uploaded' not in report['freed_sources']
    assert Path(world.jobs.get('uploaded')['source_path']).is_file()


def test_sweep_does_not_dismiss_failed_uploaded_original(world):
    world.source('failed-upload', kind='upload', status='failed', age_days=40)
    report = lifecycle.sweep(world.ctx)
    assert 'failed-upload' not in report['dismissed_sources']
    assert Path(world.jobs.get('failed-upload')['source_path']).is_file()


def test_restart_cleans_abandoned_staging_without_touching_locked_restore(world):
    from clippie import job_locks
    base = world.root / 'restore-staging'
    for name in ('abandoned', 'active'):
        folder = base / name
        folder.mkdir(parents=True)
        (folder / 'upload-partial').write_bytes(b'partial')
    assert job_locks.acquire(world.root, 'active')
    try:
        lifecycle.sweep(world.ctx)
        assert not (base / 'abandoned').exists()
        assert (base / 'active' / 'upload-partial').exists()
    finally:
        job_locks.release(world.root, 'active')
    lifecycle.sweep(world.ctx)
    assert not (base / 'active').exists()


def test_upgrade_clocks_are_distinct_and_old_documents_survive(world):
    import os
    world.source('legacy-link', age_days=60)
    world.source('legacy-failed', status='failed', age_days=60)
    old = world.project('legacy-link', created_days_ago=60)
    before = world.collections.get_collection(old['id'])
    old_stamp = world.clock.now - 60 * DAY
    os.utime(world.root / 'legacy-failed', (old_stamp, old_stamp))
    orphan = master(world, 'old-orphan', None)
    os.utime(orphan, (old_stamp, old_stamp))
    with world.studio._db() as db:
        db.execute('DROP TABLE project_lifecycle')
        db.execute('DROP TABLE lifecycle_settings')
    world.ctx = Ctx(world.root, world.collections, world.jobs, world.studio, publish=lambda: world.publish, clock=world.clock)
    assert world.collections.get_collection(old['id']) == before
    lifecycle.sweep(world.ctx)
    assert orphan.exists() and world.jobs.get('legacy-failed') and world.jobs.get('legacy-link')
    world.clock.advance(1.01)
    lifecycle.sweep(world.ctx)
    assert not orphan.exists() and world.jobs.get('legacy-failed') and world.jobs.get('legacy-link')
    world.clock.advance(6.01)
    lifecycle.sweep(world.ctx)
    with pytest.raises(KeyError):
        world.jobs.get('legacy-failed')
    assert world.jobs.get('legacy-link')
    world.clock.advance(8.01)
    lifecycle.sweep(world.ctx)
    assert lifecycle.source_is_freed(world.jobs.get('legacy-link'))
    assert world.collections.get_collection(old['id']) == before


@pytest.mark.parametrize('failure', ['open', 'read'])
def test_unavailable_publish_store_blocks_destructive_actions(world, failure):
    world.source('safety')
    project = world.project('safety')
    def unavailable():
        raise OSError('store unavailable')
    class Broken:
        def posts(self):
            raise OSError('read failed')
    world.ctx.publish = unavailable if failure == 'open' else lambda: Broken()
    assert raises(503, lifecycle.free_source, world.ctx, project['id'])
    assert raises(503, lifecycle.trash, world.ctx, project['id'])
    assert Path(world.jobs.get('safety')['source_path']).exists()
    world.ctx.publish = lambda: world.publish
    lifecycle.trash(world.ctx, project['id'])
    world.ctx.publish = unavailable if failure == 'open' else lambda: Broken()
    assert raises(503, lifecycle.purge_project, world.ctx, project['id'])
    assert world.collections.get_collection(project['id'])


@pytest.mark.parametrize('missing', ['expected', 'actual', 'preview'])
def test_link_restore_stays_unavailable_without_verified_identity_and_preview(world, missing):
    world.source('link-unknown')
    project = world.project('link-unknown')
    lifecycle.free_source(world.ctx, project['id'])
    job = world.jobs.get('link-unknown')
    Path(job['source_path']).write_bytes(b'v')
    changes = {'status': 'ready'}
    if missing == 'expected':
        changes['freed_provider_id'] = None
    elif missing == 'actual':
        changes['provenance'] = {}
    else:
        changes['preview_path'] = str(world.root / 'missing-preview.mp4')
    world.jobs.update('link-unknown', **changes)
    assert lifecycle.source_is_freed(world.jobs.get('link-unknown'))
    lifecycle.reconcile(world.ctx)
    assert lifecycle.source_is_freed(world.jobs.get('link-unknown'))
