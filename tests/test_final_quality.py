"""Final-media policy: fake downloads, real file probes, no external uploads."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from clippie import final_quality as quality


def info(width=1920, height=1080, duration=30):
    return {'width': width, 'height': height, 'duration': duration, 'has_audio': True}


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / 'proxy.mp4'
    path.write_text(json.dumps(info(1280, 720)))
    monkeypatch.setattr(quality, 'probe', lambda path: json.loads(Path(path).read_text()))
    job = {'id': 's', 'title': 'Source', 'source': 'https://video.example/watch?v=original',
           'source_path': str(path), 'duration': 30, 'provenance': {'id': 'original'},
           'options': {'quality': 'small', 'range': {'start': 10, 'end': 40}},
           'words': [{'id': 1, 'text': 'qualified speech', 'start': 1, 'end': 2}]}
    return job


def timeline():
    return {'canvas': {'width': 270, 'height': 480, 'fps': 30}, 'segments': [{'source_id': 's'}], 'broll': []}


def downloader(monkeypatch, resolution=None, media_id='original'):
    calls = []
    def download(url, directory, **kwargs):
        calls.append((url, kwargs))
        directory.mkdir(exist_ok=True)
        path = directory / 'master.mp4'
        path.write_text(json.dumps(resolution or info()))
        return path, {'id': media_id}
    monkeypatch.setattr(quality.download, 'download_file', download)
    return calls


def test_small_canvas_normalizes_a_copy_and_higher_canvas_is_preserved():
    original = timeline()
    final = quality.final_timeline(original)
    assert (final['canvas']['width'], final['canvas']['height']) == (1080, 1920)
    assert original['canvas']['width'] == 270
    original['canvas'].update(width=2160, height=3840)
    assert quality.final_timeline(original) == original


def test_web_proxy_gets_separate_identity_checked_master_and_reuses_cache(tmp_path, monkeypatch, source):
    calls = downloader(monkeypatch)
    prior = copy.deepcopy(source);proxy_bytes = Path(source['source_path']).read_bytes()
    jobs, proof = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert source == prior and Path(source['source_path']).read_bytes() == proxy_bytes
    assert jobs['s']['source_path'] != source['source_path']
    assert jobs['s']['words'] == source['words'] and proof['s']['height'] == 1080
    assert calls[0][1]['range_'] == {'start': 10, 'end': 40}
    assert '[width>=1080]' in calls[0][1]['fmt'] and '[height>=1080]' in calls[0][1]['fmt']
    again, _ = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert again['s']['source_path'] == jobs['s']['source_path'] and len(calls) == 1
    # Range and proxy identity form part of the cache key.
    other = copy.deepcopy(source);other['options']['range'] = {'start': 20, 'end': 50}
    changed, _ = quality.prepare_sources(timeline(), {'s': other}, tmp_path / 'cache')
    assert changed['s']['source_path'] != jobs['s']['source_path'] and len(calls) == 2
    Path(jobs['s']['source_path']).write_text(json.dumps(info(640, 480)))
    quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert len(calls) == 3  # A corrupt/changed cached file does not qualify.


@pytest.mark.parametrize('resolution,media_id,message', [
    (info(640, 480), 'original', 'below 1080p'),
    (info(duration=35), 'original', 'duration differs'),
    (info(), 'different-recording', 'does not match'),
    (info(1080, 1920), 'original', 'aspect differs'),
    ({**info(), 'has_audio': False}, 'original', 'missing the source audio'),
])
def test_bad_master_cannot_replace_proxy_or_qualify(tmp_path, monkeypatch, source, resolution, media_id, message):
    downloader(monkeypatch, resolution, media_id)
    original = Path(source['source_path']).read_bytes()
    with pytest.raises(quality.FinalQualityError, match=message):
        quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert Path(source['source_path']).read_bytes() == original
    assert not list((tmp_path / 'cache').glob('*/master.json'))


def test_local_hd_is_reused_but_low_local_and_unknown_identity_fail(tmp_path, monkeypatch, source):
    calls = downloader(monkeypatch)
    source['source'] = source['source_path']
    with pytest.raises(quality.FinalQualityError, match='low-resolution local'):
        quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    Path(source['source_path']).write_text(json.dumps(info()))
    jobs, _ = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert jobs['s']['source_path'] == source['source_path'] and calls == []
    source['source'] = 'https://video.example/watch';source['provenance'] = {}
    Path(source['source_path']).write_text(json.dumps(info(640, 480)))
    with pytest.raises(quality.FinalQualityError, match='Cannot verify which original'):
        quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')


def test_unavailable_hd_fails_without_silent_fallback(tmp_path, monkeypatch, source):
    def unavailable(*args, **kwargs):
        raise RuntimeError('requested format unavailable')
    monkeypatch.setattr(quality.download, 'download_file', unavailable)
    with pytest.raises(quality.FinalQualityError, match='1080p or higher version'):
        quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')


def test_shared_master_download_is_single_flight(tmp_path, monkeypatch, source):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    calls = downloader(monkeypatch)
    barrier = threading.Barrier(2)
    def prepare(_):
        barrier.wait(timeout=3)
        return quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')[0]['s']['source_path']
    with ThreadPoolExecutor(2) as workers:
        paths = list(workers.map(prepare, range(2)))
    assert paths[0] == paths[1] and len(calls) == 1


def test_source_replacement_during_render_cannot_keep_hd_provenance(tmp_path, monkeypatch, source):
    Path(source['source_path']).write_text(json.dumps(info()))
    jobs, proof = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    Path(jobs['s']['source_path']).write_text(json.dumps(info(640, 480)))
    with pytest.raises(quality.FinalQualityError, match='changed during'):
        quality.verify_sources(proof)


def artifact(path):
    return {'clip_id': 'clip', 'revision_id': 'revision', 'video_path': str(path), 'quality': '1080p',
            'width': 1920, 'height': 1080, 'sha256': quality.digest(path),
            'final_quality': {'policy': quality.POLICY, 'sources': {'s': info()}, 'duration': 30,
                              'canvas': {'width': 1920, 'height': 1080}}}


def test_gate_checks_revision_actual_dimensions_and_bytes_not_label(tmp_path, monkeypatch):
    path = tmp_path / 'file.mp4';path.write_bytes(b'original final bytes');record = artifact(path)
    monkeypatch.setattr(quality, 'probe', lambda path: info())
    quality.verify_artifact(record, short_id='clip', revision_id='revision')
    with pytest.raises(quality.FinalQualityError, match='exact revision'):
        quality.verify_artifact(record, revision_id='other')
    monkeypatch.setattr(quality, 'probe', lambda path: info(640, 480))
    with pytest.raises(quality.FinalQualityError, match='actual export'):
        quality.verify_artifact(record)
    monkeypatch.setattr(quality, 'probe', lambda path: info())
    path.write_bytes(b'replaced after rendering')
    with pytest.raises(quality.FinalQualityError, match='changed after rendering'):
        quality.verify_artifact(record)
    record.pop('final_quality')
    with pytest.raises(quality.FinalQualityError, match='provenance'):
        quality.verify_artifact(record)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')
def test_real_ffprobe_rejects_480p_even_with_hd_label(tmp_path):
    for size in ['1920x1080', '640x480']:
        path = tmp_path / (size + '.mp4')
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=blue:s={size}:r=24',
                        '-t', '0.2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True)
        record = artifact(path)
        record['final_quality']['duration'] = quality.probe(path)['duration']
        if size == '1920x1080':
            assert quality.verify_artifact(record)['height'] == 1080
        else:
            with pytest.raises(quality.FinalQualityError, match='actual export'):
                quality.verify_artifact(record)


@pytest.mark.parametrize('video,expected', [
    ({'width': 1920, 'height': 1080, 'side_data_list': [{'rotation': -90}]}, (1080, 1920)),
    ({'width': 1920, 'height': 1080, 'tags': {'rotate': '90'}}, (1080, 1920)),
    ({'width': 1440, 'height': 1080, 'sample_aspect_ratio': '4:3'}, (1920, 1080)),
])
def test_display_dimensions_match_rotated_and_anamorphic_sources(video, expected):
    from clippie.media import display_dimensions
    assert display_dimensions(video) == expected


def test_equivalent_rotated_hd_master_matches_baked_portrait_proxy(monkeypatch):
    # Encoded dimensions still satisfy HD; aspect comparison uses displayed pixels.
    monkeypatch.setattr(quality, 'probe', lambda _: {**info(), 'display_width': 1080, 'display_height': 1920})
    quality._check_master(Path('rotated.mp4'), info(720, 1280))
    monkeypatch.setattr(quality, 'probe', lambda _: {**info(1440, 1080), 'display_width': 1920, 'display_height': 1080})
    quality._check_master(Path('anamorphic.mp4'), info(1280, 720))


def mark_master(source):
    path = Path(source['source_path'])
    source['provenance'].update(master_version=1, master_sha256=quality.digest(path),
                                master_size_bytes=path.stat().st_size)


def test_imported_master_is_reused_and_never_downloaded_when_low_resolution(tmp_path, monkeypatch, source):
    calls = downloader(monkeypatch)
    mark_master(source)
    with pytest.raises(quality.FinalQualityError, match='saved source is below 1080p'):
        quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert not calls
    Path(source['source_path']).write_text(json.dumps(info()))
    mark_master(source)
    jobs, proof = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert jobs['s']['source_path'] == source['source_path']
    assert proof['s']['sha256'] == source['provenance']['master_sha256']
    assert not calls


def test_modified_or_incomplete_master_identity_fails_without_download(tmp_path, monkeypatch, source):
    calls = downloader(monkeypatch)
    mark_master(source)
    path = Path(source['source_path'])
    path.write_text(json.dumps(info(640, 720)))
    with pytest.raises(quality.FinalQualityError, match='no longer matches'):
        quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    del source['provenance']['master_sha256']
    with pytest.raises(quality.FinalQualityError, match='no longer matches'):
        quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    assert not calls


def test_same_size_same_timestamp_source_replacement_is_detected(tmp_path, monkeypatch, source):
    path = Path(source['source_path'])
    path.write_text(json.dumps(info()))
    mark_master(source)
    _, proof = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    stat = path.stat()
    data = path.read_bytes()
    path.write_bytes(data.replace(b'1920', b'1921'))
    import os
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(quality.FinalQualityError, match='changed during'):
        quality.verify_sources(proof)


def test_missing_source_during_render_is_reported_as_quality_error(tmp_path, monkeypatch, source):
    Path(source['source_path']).write_text(json.dumps(info()))
    _, proof = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    Path(source['source_path']).unlink()
    with pytest.raises(quality.FinalQualityError, match='changed during'):
        quality.verify_sources(proof)


def test_legacy_editing_source_is_checked_after_render(tmp_path, monkeypatch, source):
    downloader(monkeypatch)
    _, proof = quality.prepare_sources(timeline(), {'s': source}, tmp_path / 'cache')
    Path(source['source_path']).write_text(json.dumps(info(640, 480)))
    with pytest.raises(quality.FinalQualityError, match='changed during'):
        quality.verify_sources(proof)



def test_final_render_reads_an_hd_source_once_before_and_once_after(monkeypatch, tmp_path):
    """No master fetch: the before-render snapshot is not immediately re-hashed (previously three reads)."""
    reads = []
    real = quality.digest
    monkeypatch.setattr(quality, 'digest', lambda path: reads.append(Path(path).name) or real(path))
    source = tmp_path / 'hd.mp4'
    source.write_bytes(b'x' * 2048)
    info = {'width': 1920, 'height': 1080, 'duration': 10.0, 'has_audio': True}
    monkeypatch.setattr(quality, 'probe', lambda path: dict(info))
    monkeypatch.setattr(quality, 'hd', lambda details: True)
    timeline = {'segments': [{'source_id': 's'}], 'broll': []}
    _, proof = quality.prepare_sources(timeline, {'s': {'source_path': str(source)}}, tmp_path)
    assert reads == ['hd.mp4']
    quality.verify_sources(proof)
    assert reads == ['hd.mp4', 'hd.mp4']
