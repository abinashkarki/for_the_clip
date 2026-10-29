"""Retention curve to word mapping and the publish store."""
import pytest

from clippie.publish.retention import map_words, watch_ratio_at
from clippie.publish.store import PublishStore

WORDS = [{'text': t, 'start': i * 1.0, 'end': i * 1.0 + 0.9} for i, t in enumerate(['one', 'two', 'three', 'four', 'five'])]
CURVE = [[0.0, 1.0, 0.5], [0.4, 0.9, 0.5], [0.6, 0.4, 0.1], [1.0, 0.3, 0.2]]


def test_interpolates_watch_ratio_between_points():
    assert watch_ratio_at(CURVE, 0.0) == 1.0 and watch_ratio_at(CURVE, 1.0) == 0.3
    assert watch_ratio_at(CURVE, 0.5) == pytest.approx(0.65) and watch_ratio_at(CURVE, 2.0) == 0.3


def test_words_get_ratio_by_program_time_and_the_cliff_is_flagged():
    mapped = map_words(CURVE, WORDS, 5.0)
    by_text = {w['text']: w for w in mapped['words']}
    assert by_text['three']['ratio'] == 0.4 and by_text['three']['start'] == 2.0
    assert by_text['one']['watch_ratio'] > by_text['five']['watch_ratio']
    assert [mapped['words'][i]['text'] for i in mapped['drops']] == ['three']  # 0.4 -> 0.6 is the cliff
    assert by_text['three']['relative'] is not None


def test_missing_data_gives_empty_mapping_and_none_relative_is_kept():
    assert map_words([], WORDS, 5.0) == {'duration': 5.0, 'words': [], 'drops': []}
    assert map_words(CURVE, [], 5.0)['words'] == [] and map_words(CURVE, WORDS, 0)['words'] == []
    mapped = map_words([[0, 1.0, None], [1, 0.5, None]], WORDS, 5.0)
    assert mapped['words'][0]['relative'] is None


def test_flat_curve_flags_nothing():
    flat = [[0.0, 0.8, 0.5], [1.0, 0.8, 0.5]]
    assert map_words(flat, WORDS, 5.0)['drops'] == []


def test_store_keeps_tokens_out_of_sqlite_and_validates_fields(tmp_path):
    store = PublishStore(tmp_path)
    channel = store.add_channel('youtube', 'youtube', 'Me', 'UC1', ['s'], 'youtube:x:refresh')
    assert store.add_channel('youtube', 'youtube', 'Me renamed', 'UC1', ['s'], 'youtube:x:refresh')['id'] == channel['id']
    assert len(store.channels()) == 1 and store.channels()[0]['display_name'] == 'Me renamed'
    columns = {r[1] for r in store._run('PRAGMA table_info(channel_accounts)')}
    assert not columns & {'access_token', 'refresh_token', 'token'} and 'token_ref' in columns
    post = store.create_post(platform='youtube', channel_id=channel['id'], short_id='s', revision_id='r', title='T', status='draft')
    with pytest.raises(ValueError):
        store.update_post(post['id'], bogus=1)
    with pytest.raises(KeyError):
        store.update_post('missing', title='x')
    store.add_snapshot(post['id'], {'views': 1}, 10.0)
    store.add_snapshot(post['id'], {'views': 2}, 20.0)
    assert store.latest_snapshot(post['id'])['metrics'] == {'views': 2}
    store.delete_post(post['id'])
    assert store.snapshots(post['id']) == []


def test_oauth_state_is_single_use_and_expires(tmp_path):
    import time
    store = PublishStore(tmp_path)
    store.put_state('s1', 'youtube', 'v', 'http://127.0.0.1/cb')
    assert store.pop_state('s1')['verifier'] == 'v' and store.pop_state('s1') is None
    store._run('INSERT INTO oauth_states VALUES (?,?,?,?,?)', ('old', 'youtube', 'v', 'u', time.time() - 1000), True)
    assert store.pop_state('old') is None
