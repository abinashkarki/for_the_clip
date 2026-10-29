import json
import os
import shutil
import subprocess

import pytest
from PIL import Image

from clippie import source_visuals as visuals


@pytest.fixture
def source(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg required')
    path = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=640x360:r=2:d=1', '-f', 'lavfi', '-i', 'color=blue:s=640x360:r=2:d=1', '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0', '-pix_fmt', 'yuv420p', str(path)], check=True)
    return {'id': 'source-a', 'source_path': str(path), 'duration': 2.0}


def test_extract_color_and_reuse(source, tmp_path, monkeypatch):
    result = visuals.prepare_samples(source, tmp_path / 'cache', [0.0, 1.5])
    assert result['cache_status'] == 'build'
    for row, channel in zip(result['artifact']['samples'], [0, 2]):
        with Image.open(row['path']) as image:
            assert image.size == (320, 180)
            assert image.getpixel((160, 90))[channel] > 200
    monkeypatch.setattr(visuals.render, '_run', lambda *a, **k: pytest.fail('cache spawned FFmpeg'))
    assert visuals.prepare_samples(source, tmp_path / 'cache', [0.0, 1.5])['artifact'] == result['artifact']


def test_invalidation(source, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    first = visuals.prepare_samples(source, cache, [0.0])
    stat = os.stat(source['source_path'])
    os.utime(source['source_path'], ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    second = visuals.prepare_samples(source, cache, [0.0])
    monkeypatch.setattr(visuals, 'IMPLEMENTATION_VERSION', 2)
    third = visuals.prepare_samples(source, cache, [0.0])
    assert len({r['cache_artifact_key'] for r in [first, second, third]}) == 3
    assert first['path'].exists()


@pytest.mark.parametrize('times', [[], [0]*5, [True], [float('nan')], [float('inf')], [-1], [2], [0, 0.0]])
def test_bounds(tmp_path, times):
    with pytest.raises(ValueError):
        visuals.prepare_samples({'id': 's', 'duration': 2}, tmp_path, times)


@pytest.mark.parametrize('damage', ['missing', 'path', 'hash', 'manifest'])
def test_corrupt_preserved(source, tmp_path, damage):
    result = visuals.prepare_samples(source, tmp_path / 'cache', [0.0])
    artifact = result['artifact']
    image = artifact['samples'][0]['path']
    if damage == 'missing':
        os.unlink(image)
    elif damage == 'path':
        artifact['samples'][0]['path'] = '/outside/private.jpg'
    elif damage == 'hash':
        artifact['samples'][0]['sha256'] = 'bad'
    else:
        artifact['manifest']['source_id'] = 'wrong'
    result['path'].write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match='Corrupt visual evidence cache'):
        visuals.prepare_samples(source, tmp_path / 'cache', [0.0])
    assert result['path'].exists()


def test_cancel_cleans_partial(source, tmp_path, monkeypatch):
    class Cancelled(Exception):
        pass
    class Owner:
        cancelled = False
        def checkpoint(self):
            if self.cancelled:
                raise Cancelled()
    owner = Owner()
    original = visuals.render._run
    def cancel(*args, **kwargs):
        assert kwargs['timeout'] <= 20
        original(*args, **kwargs)
        owner.cancelled = True
    # Simulate cancellation directly after extraction without requiring process registry.
    from contextlib import nullcontext
    monkeypatch.setattr(visuals.render, 'process_ownership', lambda _: nullcontext())
    monkeypatch.setattr(visuals.render, '_run', cancel)
    with pytest.raises(Cancelled):
        visuals.prepare_samples(source, tmp_path / 'cache', [0], owner)
    assert not list((tmp_path / 'cache').glob('*/manifest.json'))
    assert not list((tmp_path / 'cache').glob('.visuals-*'))


def test_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError, match='media does not exist'):
        visuals.prepare_samples({'id': 's', 'duration': 2, 'source_path': str(tmp_path / 'missing')}, tmp_path, [0])


def test_load_without_source_and_reject_unsafe(source, tmp_path):
    root = tmp_path / 'cache'
    built = visuals.prepare_samples(source, root, [0])
    os.unlink(source['source_path'])
    loaded = visuals.load_samples(root, built['cache_artifact_key'])
    assert loaded['artifact'] == built['artifact']
    for key in ['../outside', 'A'*64, 'x'*64, 'a'*63]:
        with pytest.raises(ValueError, match='fingerprint'):
            visuals.load_samples(root, key)
    image_path = built['artifact']['samples'][0]['path']
    os.unlink(image_path)
    os.symlink(tmp_path / 'outside', image_path)
    with pytest.raises(ValueError, match='Corrupt'):
        visuals.load_samples(root, built['cache_artifact_key'])
