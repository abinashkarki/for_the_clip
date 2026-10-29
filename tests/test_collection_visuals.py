import copy
import json
import os
import subprocess

import pytest

from clippie import collection_visuals as visuals, render


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=10:d=4',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True, capture_output=True)
    return {'id': 's', 'source_path': str(path), 'duration': 4., 'words': []}


def retrieve(tmp_path, source):
    return visuals.for_candidate(tmp_path / 'cache', tmp_path / 'data', {'s': source},
        {'source_roles': {'s': 'picture_only'}}, {'title': 'Testing pictures', 'angle': 'A complete idea'})


def test_coverage_is_labelled_bounded_and_cached(tmp_path, source, monkeypatch):
    rows, info = retrieve(tmp_path, source)
    assert info['mode'] == 'coverage_samples' and 'No prepared semantic' in info['reason']
    assert len(rows) == 2 and all(row['source_id'] == 's' and row['end'] - row['start'] <= 2 for row in rows)
    assert all(set(row['picture']) == {'id', 'source', 'at', 'data'} for row in rows)
    monkeypatch.setattr(render, '_run', lambda *a, **kw: pytest.fail('Cache repeated extraction'))
    assert retrieve(tmp_path, source)[0] == rows
    proof = [{k: v for k, v in row.items() if k != 'picture'} for row in rows]
    assert visuals.for_plan(tmp_path / 'cache', {'visual_evidence': proof}) == rows
    changed = copy.deepcopy(proof); changed[0]['end'] -= .1
    with pytest.raises(ValueError, match='differs'):
        visuals.for_plan(tmp_path / 'cache', {'visual_evidence': changed})


def test_existing_current_semantic_index_retrieves_images_only(tmp_path, source, monkeypatch):
    from clippie import analysis
    directory = tmp_path / 'data/s/analysis'; directory.mkdir(parents=True)
    (directory / 'analysis.json').write_text(json.dumps({'source_id': 's', 'semantic': True,
        'fingerprint': analysis.source_fingerprint(source), 'model_revision': analysis.MODEL_REVISION}))
    monkeypatch.setattr(analysis, 'semantic_status', lambda: {'installed': True, 'weights_ready': True})
    calls = []
    def search(indexes, query, **kwargs):
        calls.append((indexes, query, kwargs))
        return [{'source_id': 'outside', 'at': 1}, {'source_id': 's', 'at': 2.2}]
    monkeypatch.setattr(analysis, 'search', search)
    rows, info = retrieve(tmp_path, source)
    assert info['mode'] == 'semantic_index' and len(rows) == 1
    assert rows[0]['at'] == 2.2 and rows[0]['source_id'] == 's'
    assert calls[0][2] == {'limit': 12, 'use_embeddings': True, 'modality': 'image'}


def test_outdated_index_is_explicit_and_never_searched(tmp_path, source, monkeypatch):
    from clippie import analysis
    directory = tmp_path / 'data/s/analysis'; directory.mkdir(parents=True)
    (directory / 'analysis.json').write_text(json.dumps({'source_id': 's', 'semantic': True,
        'fingerprint': 'outdated', 'model_revision': analysis.MODEL_REVISION}))
    monkeypatch.setattr(analysis, 'search', lambda *a, **kw: pytest.fail('Outdated index used'))
    rows, info = retrieve(tmp_path, source)
    assert len(rows) == 2 and info['mode'] == 'coverage_samples' and 'outdated' in info['reason']


def test_corrupt_index_fails_visibly(tmp_path, source):
    directory = tmp_path / 'data/s/analysis'; directory.mkdir(parents=True)
    (directory / 'analysis.json').write_text('broken JSON')
    with pytest.raises(ValueError, match='damaged'):
        retrieve(tmp_path, source)


def test_picture_identity_changes_with_media_stat(tmp_path, source):
    before = visuals.source_snapshot(source)
    path = tmp_path / 'source.mp4'; stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert visuals.source_snapshot(source) != before


def test_empty_scope_and_excessive_saved_proof_rejected(tmp_path):
    with pytest.raises(ValueError, match='scope'):
        visuals.for_candidate(tmp_path, tmp_path, {}, {}, {})
    with pytest.raises(ValueError, match='at most two'):
        visuals.for_plan(tmp_path, {'visual_evidence': [{}, {}, {}]})
