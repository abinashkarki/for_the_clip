import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from clippie import collection_media, render


def context():
    processes = []
    return SimpleNamespace(task={'id': 'task', 'short_id': 'short', 'lease_token': 'lease'},
                           checkpoint=lambda: None, register_process=lambda p: processes.append(p),
                           processes=processes)


def revision():
    return {'id': 'revision', 'short_id': 'short', 'document': {
        'editorial': {'status': 'pass'}, 'technical': 'pass',
        'timeline': {'schema_version': 2, 'aspect_ratio': '9:16', 'width': 1080, 'height': 1920,
                     'clips': [{'id': 'clip', 'source_id': 'source', 'start': 0, 'end': 2,
                                'framing': {'mode': 'fill'}}]}}}


def test_actual_preview_owned_immutable_and_validated(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg tools required')
    path = tmp_path / 'fixture.mp4'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=red:s=320x180:r=30:d=2', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000:duration=2',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)],
                   check=True, capture_output=True)
    sources = {'source': {'source_path': path, 'duration': 2, 'words': []}}
    ctx, rev = context(), revision()
    original = copy.deepcopy(rev)
    artifact = collection_media.render_revision(ctx, rev, sources, tmp_path)
    assert rev == original
    assert artifact['profile']['width'] == 360 and artifact['profile']['height'] == 640
    assert artifact['probe']['has_audio'] and artifact['probe']['duration'] == pytest.approx(2, abs=.15)
    assert artifact['sha256'] == hashlib.sha256(Path(artifact['video_path']).read_bytes()).hexdigest()
    assert len(ctx.processes) >= 5 and all(p.poll() is not None for p in ctx.processes)
    json.dumps(artifact)
    with pytest.raises(FileExistsError):
        collection_media.render_revision(ctx, rev, sources, tmp_path)


def test_rejects_unreviewed_or_wrong_short_before_files(tmp_path):
    ctx, rev = context(), revision()
    rev['document']['editorial']['status'] = 'uncertain'
    with pytest.raises(ValueError):
        collection_media.render_revision(ctx, rev, {}, tmp_path)
    rev['document']['editorial']['status'] = 'pass'
    ctx.task['short_id'] = 'another'
    with pytest.raises(ValueError):
        collection_media.render_revision(ctx, rev, {}, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cancelled_registration_terminates_child(monkeypatch):
    class Process:
        returncode = None
        terminated = False
        def poll(self): return self.returncode
        def terminate(self): self.terminated = True; self.returncode = -15
        def wait(self, timeout): return self.returncode
    process = Process()
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: process)
    def cancel(_): raise RuntimeError('lease_lost')
    owner = SimpleNamespace(checkpoint=lambda: None, register_process=cancel)
    with render.process_ownership(owner), pytest.raises(RuntimeError, match='lease_lost'):
        render._run(['ffprobe'], timeout=30, what='probe')
    assert process.terminated
    assert render._PROCESS_OWNER.get() is None


def test_export_preserves_canonical_profile_and_composition(tmp_path, monkeypatch):
    ctx, rev = context(), revision()
    timeline = copy.deepcopy(rev['document']['timeline'])
    def fake_render(value, sources, directory, process_owner=None):
        assert value == timeline
        assert process_owner is ctx
        video = directory / 'production.mp4'
        video.write_bytes(b'validated-by-mocked-probe')
        return {'video_path': video, 'srt_path': directory / 'production.srt',
                'manifest_path': directory / 'manifest.json', 'metrics': {'duration': 2}, 'warnings': []}
    monkeypatch.setattr(render, 'render_production', fake_render)
    monkeypatch.setattr(render, '_probe', lambda _: {'duration': 2, 'width': 1080, 'height': 1920,
                                                    'has_audio': True, 'video_codec': 'h264'})
    artifact = collection_media.render_revision(ctx, rev, {}, tmp_path, 'export')
    assert artifact['profile']['width'] == 1080 and artifact['profile']['height'] == 1920
    assert rev['document']['timeline'] == timeline
