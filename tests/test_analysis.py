import json
from pathlib import Path
import numpy as np
import pytest
from clippie import analysis


def test_semantic_search_uses_stored_vectors_and_never_substitutes_keywords(tmp_path,monkeypatch):
    items=[{'id':'a','source_id':'s','start':0,'end':2,'text':'unrelated wording','modality':'image'},
           {'id':'b','source_id':'s','start':3,'end':4,'text':'carving stone','modality':'text'}]
    (tmp_path/'analysis.json').write_text(json.dumps({'semantic':True,'items':items}))
    vectors=np.zeros((2,768),dtype=np.float32);vectors[0,0]=1;vectors[1,1]=1
    np.save(tmp_path/'embeddings.npy',vectors)
    monkeypatch.setattr(analysis,'encode',lambda items,query=False:np.eye(1,768,dtype=np.float32))
    results=analysis.search([tmp_path],'carving stone',use_embeddings=True)
    assert results[0]['id']=='a' and results[0]['provider']=='google/embeddinggemma-2'
    assert analysis.search([tmp_path],'carving stone',use_embeddings=False)[0]['id']=='b'
    (tmp_path/'analysis.json').write_text(json.dumps({'semantic':False,'items':items}))
    with pytest.raises(ValueError,match='no semantic index'):
        analysis.search([tmp_path],'carving stone',use_embeddings=True)


def test_passages_preserve_source_speech_and_fingerprint_changes(tmp_path):
    path=tmp_path/'source.mp4';path.write_bytes(b'media')
    source={'id':'s','duration':20.,'source_path':str(path),'words':[
        {'id':0,'text':'Question?','start':0.,'end':2.}, {'id':1,'text':'Answer.','start':10.,'end':13.}]}
    p=analysis.passages(source)
    assert [i['text'] for i in p]==['Question?','Answer.']
    original=analysis.source_fingerprint(source)
    source['words'][0]['text']='Changed?'
    assert analysis.source_fingerprint(source)!=original


def test_story_retrieval_keeps_beat_evidence_separate_and_excludes_images(tmp_path, monkeypatch):
    items = [{'id': str(i), 'source_id': 's', 'start': i*5, 'end': i*5+4,
              'text': text, 'modality': modality} for i, (text, modality) in enumerate([
                  ('A question opens the story.', 'text'), ('An answer closes the story.', 'text'),
                  ('A picture that matches every query.', 'image')])]
    (tmp_path/'analysis.json').write_text(json.dumps({'semantic': True, 'items': items}))
    vectors = np.zeros((3, 768), dtype=np.float32)
    vectors[0, :3] = [.5, 1, 0]; vectors[1, :3] = [.5, 0, 1]; vectors[2, :3] = [1, 1, 1]
    np.save(tmp_path/'embeddings.npy', vectors)
    queries = []
    def encode(items, query=False):
        queries.extend(items)
        assert query
        return np.eye(3, 768, dtype=np.float32)
    monkeypatch.setattr(analysis, 'encode', encode)
    beats = [{'query': 'question', 'purpose': 'Open a question'}, {'query': 'answer', 'purpose': 'Resolve it'}]
    results = analysis.story_material([tmp_path], 'The brief', beats)
    assert len(results) == 2 and all(row['modality'] == 'text' for row in results)
    by_id = {row['id']: row for row in results}
    assert by_id['0']['beat_scores'] == [1., 0.]
    assert by_id['1']['beat_scores'] == [0., 1.]
    assert queries == ['The brief', 'question. Open a question', 'answer. Resolve it']

    vectors[0, 0] = float('nan'); np.save(tmp_path/'embeddings.npy', vectors)
    with pytest.raises(ValueError, match='reindex'):
        analysis.story_material([tmp_path], 'The brief', beats)
