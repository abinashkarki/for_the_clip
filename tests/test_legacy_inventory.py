import json
import sqlite3

from scripts.inventory_legacy import compare, inventory


def test_read_only_inventory_detects_changed_media_and_docs(tmp_path):
    media = tmp_path / 'source.mp4'
    media.write_bytes(b'original')
    with sqlite3.connect(tmp_path / 'clippie.sqlite3') as db:
        for table in ('jobs', 'productions'):
            db.execute(f'CREATE TABLE {table} (id TEXT PRIMARY KEY, document TEXT)')
        db.execute('INSERT INTO jobs VALUES (?,?)', ('s1', json.dumps({
            'id': 's1', 'source': 'private-source-url', 'source_path': str(media),
            'words': [{'text': 'private transcript'}]})))
        db.execute('INSERT INTO productions VALUES (?,?)', ('p1', json.dumps({
            'id': 'p1', 'revision': 2, 'render': {'id': 'r1', 'status': 'ready', 'video_path': str(media)}})))
    before = inventory(tmp_path)
    assert 'private-source-url' not in json.dumps(before)
    assert 'private transcript' not in json.dumps(before)
    assert before['productions'][0]['render_urls'] == ['/api/productions/p1/video?render_id=r1']
    assert compare(before, inventory(tmp_path)) == []
    media.write_bytes(b'changed')
    assert len(compare(before, inventory(tmp_path))) == 1
    with sqlite3.connect(tmp_path / 'clippie.sqlite3') as db:
        db.execute("UPDATE productions SET document=? WHERE id='p1'", (json.dumps({'id': 'p1', 'revision': 3}),))
    assert len(compare(before, inventory(tmp_path))) == 2
