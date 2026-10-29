"""Posts carry their project: migration, live resolution, trashed and purged projects."""
import sqlite3

from publish_support import finished_post, publish, studio, fake_engine, video_bytes, connect_youtube  # noqa: F401

from clippie import studio_api
from clippie.publish.store import PublishStore, snapshot_project_names

API = '/api/publish'


def edit_collection(short, **changes):
    store = studio_api.collections()
    collection = store.get_collection(short['collection_id'])
    with store._transaction(write=True) as db:
        store._save(db, 'collection', {**collection, **changes})
    return collection


def delete_short_records(short):
    store = studio_api.collections()
    with store._transaction(write=True) as db:
        db.execute('DELETE FROM records WHERE id=?', (short['id'],))
        db.execute('DELETE FROM records WHERE id=?', (short['collection_id'],))


def draft(publish):
    connect_youtube(publish)
    return finished_post(publish, draft=True)


def listed(publish):
    return publish.studio.client.get(f'{API}/posts').json()['posts'][0]


def test_migration_adds_columns_once_and_keeps_rows(tmp_path):
    path = tmp_path / 'publish.sqlite3'
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE posts (id TEXT PRIMARY KEY, platform TEXT NOT NULL, channel_id TEXT NOT NULL,
      short_id TEXT NOT NULL, revision_id TEXT NOT NULL, artifact_id TEXT, render_job_id TEXT, title TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '', hashtags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL, scheduled_at REAL,
      published_at REAL, external_id TEXT, url TEXT, error TEXT, warnings TEXT NOT NULL DEFAULT '[]', duration REAL,
      upload_session TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL, poll_stage INTEGER NOT NULL DEFAULT 0,
      next_poll_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
      INSERT INTO posts (id,platform,channel_id,short_id,revision_id,title,status,created_at,updated_at)
      VALUES ('p1','youtube','c','s','r','T','draft',1,1);''')
    db.commit()
    db.close()
    for _ in range(2):
        store = PublishStore(tmp_path)
    post = store.get_post('p1')
    assert post['project_id'] is None and post['project_name'] is None and post['title'] == 'T'


def test_new_post_snapshots_project_and_lists_it_live(publish):
    post, short, _ = draft(publish)
    assert post['project_id'] == short['collection_id'] and post['project_name']
    item = listed(publish)
    assert item['project_id'] == short['collection_id'] and item['project_deleted'] is False
    edit_collection(short, name='Renamed live')
    item = listed(publish)
    assert item['project_name'] == 'Renamed live'
    assert publish.db.get_post(post['id'])['project_name'] == 'Renamed live'


def test_trashed_project_uses_snapshot_and_is_flagged(publish):
    post, short, _ = draft(publish)
    edit_collection(short, name='Before trash')
    listed(publish)
    edit_collection(short, name='After trash', trashed_at='2026-10-28T00:00:00Z')
    item = listed(publish)
    assert (item['project_id'], item['project_name'], item['project_deleted']) == (short['collection_id'], 'Before trash', True)


def test_missing_collection_falls_back_to_snapshot_and_old_posts_backfill(publish):
    post, short, _ = draft(publish)
    publish.db.set_project(post['id'], None, None)       # a post from before the migration
    assert listed(publish)['project_id'] == short['collection_id']  # lazily backfilled and persisted
    assert publish.db.get_post(post['id'])['project_id'] == short['collection_id']
    delete_short_records(short)
    item = listed(publish)
    assert item['project_deleted'] is True and item['project_id'] == short['collection_id'] and item['project_name']


def test_never_snapshotted_post_with_missing_short_has_no_project(publish):
    post, short, _ = draft(publish)
    publish.db.set_project(post['id'], None, None)
    delete_short_records(short)
    item = listed(publish)
    assert (item['project_id'], item['project_name'], item['project_deleted']) == (None, None, True)


def test_snapshot_project_names_for_purge(publish):
    post, short, _ = draft(publish)
    publish.db.set_project(post['id'], None, None)
    assert snapshot_project_names(short['collection_id'], 'Frozen', [short['id']], store=publish.db) == 1
    delete_short_records(short)
    item = listed(publish)
    assert item['project_name'] == 'Frozen' and item['project_deleted'] is True
