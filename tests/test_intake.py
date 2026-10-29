"""Source intake: staged progress, lanes, audio-first download, cancel, retry, stall, recovery. No network."""
from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import subprocess
import threading
import time
import types
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from clippie import app as module, download, intake, media
from clippie import intake_progress as progress
from clippie.store import Store

FFMPEG = shutil.which('ffmpeg')
API = '/api/studio'
URL = 'https://video.example/watch?v=abc'
needs_ffmpeg = pytest.mark.skipif(not FFMPEG or not shutil.which('ffprobe'), reason='FFmpeg tools are required')


def wait_for(predicate, timeout=20.0, what='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail(f'Timed out waiting for {what}')


def ffmpeg(*args):
    subprocess.run([FFMPEG, '-y', '-v', 'error', *args], check=True, capture_output=True)


@pytest.fixture(scope='session')
def samples(tmp_path_factory):
    if not FFMPEG:
        pytest.skip('FFmpeg is required')
    root = tmp_path_factory.mktemp('samples')
    audio, video, both = root / 'audio.m4a', root / 'video.mp4', root / 'both.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=440:duration=6', '-c:a', 'aac', str(audio))
    ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=24:duration=6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video))
    ffmpeg('-i', str(video), '-i', str(audio), '-c', 'copy', str(both))
    return types.SimpleNamespace(audio=audio, video=video, both=both)


WORDS = [{'id': i, 'text': t, 'start': i * .5, 'end': i * .5 + .4}
         for i, t in enumerate('Better decisions start here and then you learn from reality.'.split())]


class FakeYtDlp:
    """A stand-in for the yt_dlp module that writes real media files and drives the progress hooks."""

    def __init__(self, samples):
        self.samples = samples
        self.log: list[tuple] = []
        self.options: list[dict] = []
        self.gates = {'audio': threading.Event(), 'video': threading.Event(), 'source': threading.Event()}
        for gate in self.gates.values():
            gate.set()
        self.fail = {}
        self.ticks = 5
        self.info = {'id': 'abc', 'title': 'A long talk', 'duration': 6.0, 'thumbnail': 'https://img.example/t.jpg',
                     'uploader': 'Channel', 'webpage_url': URL,
                     'formats': [{'format_id': 'a', 'acodec': 'mp4a', 'vcodec': 'none', 'filesize': 1_000_000},
                                 {'format_id': 'v', 'acodec': 'none', 'vcodec': 'avc1', 'height': 720, 'fps': 30, 'filesize': 9_000_000}]}
        controller = self

        class DownloadCancelled(Exception):
            pass

        class YoutubeDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True, process=True):
                if not download:
                    return dict(controller.info)
                return controller.download(self.opts)

            def process_ie_result(self, info, download=False):
                fmt = self.opts['format']
                if fmt.startswith('bestaudio'):
                    return {**info, 'requested_formats': [info['formats'][0]]}
                return {**info, 'requested_formats': [info['formats'][1]]}

        self.utils = types.SimpleNamespace(DownloadCancelled=DownloadCancelled)
        self.YoutubeDL = YoutubeDL

    def download(self, opts):
        template = Path(opts['outtmpl'])
        stem, directory = template.name.split('.')[0], template.parent
        self.options.append(opts)
        self.log.append(('start', stem))
        source = {'audio': self.samples.audio, 'video': self.samples.video, 'source': self.samples.both}[stem]
        target = directory / f'{stem}{source.suffix}'
        part = Path(f'{target}.part')
        total = 1_000_000
        if stem in self.fail:
            raise RuntimeError(self.fail[stem])
        part.write_bytes(b'partial')
        for tick in range(1, self.ticks + 1):
            for hook in opts['progress_hooks']:
                hook({'status': 'downloading', 'filename': str(target), 'downloaded_bytes': total * tick / self.ticks,
                      'total_bytes': total, 'speed': 2_000_000, 'eta': 1})
        while not self.gates[stem].is_set():
            for hook in opts['progress_hooks']:
                hook({'status': 'downloading', 'filename': str(target), 'downloaded_bytes': total, 'total_bytes': total,
                      'speed': 2_000_000, 'eta': 1})
            time.sleep(0.01)
        shutil.copy(source, target)
        part.unlink(missing_ok=True)
        for hook in opts['progress_hooks']:
            hook({'status': 'finished', 'filename': str(target), 'downloaded_bytes': total, 'total_bytes': total})
        self.log.append(('end', stem))
        return {'requested_downloads': [{'filepath': str(target)}]}


@pytest.fixture
def fake(monkeypatch, samples):
    controller = FakeYtDlp(samples)
    monkeypatch.setattr(download, '_ytdlp', lambda: controller)
    return controller


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'STORE', Store(tmp_path / 'data'))
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(module, 'KEYS', {'deepseek': '', 'groq': 'k'})

    def select(job_id, request):
        module.STORE.update(job_id, status='selecting', progress='Finding self-contained candidate clips…')
        module.STORE.update(job_id, status='ready', clips=[{'id': 'c1', 'start': 0, 'end': 4}], progress='1 candidates ready to review.')
    monkeypatch.setattr(module, 'do_select', select)
    with TestClient(module.app, headers={'X-Clippie-Observation': 'qa'}) as client:
        yield client
    module.WORKER.shutdown(wait=True)


def fake_transcribe(log, delay=0.0, gate=None):
    def transcribe(source, directory, provider='auto', language=None, groq_key=None, transcript_path=None, *, hooks=None):
        log.append(('transcribe-start', Path(source).suffix))
        if hooks and hooks.started:
            hooks.started(0.0, 6.0)
        if gate:
            gate.wait(10)
        time.sleep(delay)
        if hooks and hooks.finished:
            hooks.finished(6.0, 6.0, WORDS)
        log.append(('transcribe-end',))
        return {'words': [dict(w) for w in WORDS], 'segments': [], 'provider': 'groq', 'model': 'm', 'warnings': []}
    return transcribe


def add_url(client, **extra):
    response = client.post(f'{API}/sources', json={'url': URL, **extra})
    assert response.status_code == 202, response.text
    return response.json()['source_id']


def source(client, source_id):
    return next(s for s in client.get(f'{API}/sources').json() if s['id'] == source_id)


def stage(summary, stage_id):
    return next(s for s in summary['stages'] if s['id'] == stage_id)


# -- text and format selection ------------------------------------------------------

def test_download_detail_reads_like_the_progress_a_person_wants():
    assert progress.download_detail(1.83e9, 3.13e9, 3.4e6, 380) == '1.83 of 3.13 GB · 3.4 MB/s · ~6 min left'
    assert progress.download_detail(412.5e6, 880.1e6, 900e3, None) == '412.5 of 880.1 MB · 900 KB/s'
    assert progress.download_detail(5e6, None, None, None) == '5.0 MB downloaded'
    assert progress.format_eta(20) == '~20 s left' and progress.format_eta(125) == '~2 min left'
    assert progress.format_eta(4320) == '~1 h 12 min left' and progress.format_eta(7200) == '~2 h left'
    assert progress.format_eta(None) == '' and progress.format_eta(float('nan')) == ''


def test_format_selection_uses_resolution_caps(monkeypatch):
    monkeypatch.delenv('CLIPPIE_DOWNLOAD_HEIGHT', raising=False)
    assert download.video_format('standard') == 'bestvideo[height<=1080]/best[height<=1080]'
    assert download.video_format('high') == 'bestvideo[height<=2160]/best[height<=2160]'
    assert download.video_format('small') == 'bestvideo[height<=480]/best[height<=480]'
    assert download.audio_format().startswith('bestaudio[ext=m4a]')
    assert download.single_format('standard') == 'bestvideo[height<=1080]+bestaudio/best[height<=1080]'
    monkeypatch.setenv('CLIPPIE_DOWNLOAD_HEIGHT', '360')
    assert download.max_height('standard') == 360 and download.max_height('small') == 360


def test_format_sort_prefers_h264_then_thirty_fps_within_the_cap():
    # Renders are 30 fps: a 1080p60 stream would double the download and hashing for no gain.
    assert download.base_options()['format_sort'] == ['res', 'vcodec:h264', 'fps:30']


def test_master_provenance_and_storage_preflight(tmp_path, monkeypatch):
    import hashlib
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'master')
    provenance = intake.master_provenance(source)
    assert provenance == {'master_version': 1, 'master_sha256': hashlib.sha256(b'master').hexdigest(), 'master_size_bytes': 6}
    monkeypatch.setattr(intake.shutil, 'disk_usage', lambda _: types.SimpleNamespace(free=29))
    with pytest.raises(ValueError, match='Not enough free disk space'):
        intake.check_free_space(tmp_path, 10)
    intake.check_free_space(tmp_path, None)


def test_stream_identity_and_duration_drift_fail(monkeypatch):
    store = types.SimpleNamespace(get=lambda _: {'provenance': {'id': 'same', 'remote_duration': 3600}})
    env = types.SimpleNamespace(store=store)
    tracker = types.SimpleNamespace(job_id='job')
    intake.validate_remote_identity(env, tracker, {'id': 'same', 'duration': 3600})
    with pytest.raises(ValueError, match='duration changed'):
        intake.validate_remote_identity(env, tracker, {'id': 'same', 'duration': 3616})
    with pytest.raises(ValueError, match='does not match'):
        intake.validate_remote_identity(env, tracker, {'id': 'other', 'duration': 3600})
    monkeypatch.setattr(media, 'probe_duration', lambda p: 3600 if str(p) == 'video' else 3616)
    tracker.check = lambda: None
    with pytest.raises(ValueError, match='durations differ'):
        intake.join_streams(tracker, Path('.'), Path('video'), Path('audio'))


def test_range_option_uses_yt_dlp_sections_with_keyframe_cuts(tmp_path):
    opts = download.download_options(tmp_path, 'video', 'best', merge=False, range_={'start': 60, 'end': 180.5},
                                     progress_hook=None, postprocessor_hook=None)
    assert opts['force_keyframes_at_cuts'] is True
    assert list(opts['download_ranges']({'duration': 600}, None)) == [{'start_time': 60.0, 'end_time': 180.5}]
    plain = download.download_options(tmp_path, 'video', 'best', merge=True, range_=None, progress_hook=None, postprocessor_hook=None)
    assert 'download_ranges' not in plain and plain['merge_output_format'] == 'mp4'
    assert plain['continuedl'] is True and plain['outtmpl'] == str(tmp_path / 'video.%(ext)s')


# -- stages, throttling and stalls ---------------------------------------------------------

def test_progress_hook_becomes_stage_fraction_and_detail(tmp_path):
    store = Store(tmp_path)
    job = store.save({'id': 'j', 'status': 'ingesting', 'progress': ''})
    tracker = progress.Tracker.open(store, 'j')
    tracker.plan(['download', 'merge', 'preview', 'transcribe', 'index'])
    tracker.start('download', 'Starting')
    meter = intake.DownloadMeter(tracker, expected_total=3.13e9)
    meter.hook({'status': 'downloading', 'filename': 'video.mp4', 'downloaded_bytes': 1.83e9, 'total_bytes': 2.98e9,
                'speed': 3.4e6, 'eta': 300})
    tracker.flush(force=True)
    saved = store.get('j')
    active = next(s for s in saved['stages'] if s['id'] == 'download')
    assert active['detail'] == '1.83 of 3.13 GB · 3.4 MB/s · ~6 min left'
    assert active['fraction'] == pytest.approx(1.83 / 3.13, abs=1e-3) and active['eta_seconds'] == pytest.approx(1.3e9 / 3.4e6)
    assert 0 < saved['overall'] < 0.35 and saved['status'] == 'ingesting'
    assert saved['progress'].startswith('Download · 1.83 of 3.13 GB')


def test_overall_fraction_is_weighted_monotonic_and_reaches_one_only_on_completion(tmp_path):
    store = Store(tmp_path)
    store.save({'id': 'j', 'status': 'queued'})
    tracker = progress.Tracker.open(store, 'j')
    tracker.plan(['preview', 'transcribe', 'index'], weights={'preview': 1, 'transcribe': 8, 'index': 1})
    seen = []
    tracker.start('preview')
    tracker.skip('preview', 'Already plays in the browser')
    seen.append(tracker.overall())
    tracker.start('transcribe')
    tracker.progress('transcribe', 0.5)
    seen.append(tracker.overall())
    tracker.progress('transcribe', 0.2)       # a lower reading never moves the bar backwards
    seen.append(tracker.overall())
    tracker.finish('transcribe')
    tracker.start('index')
    tracker.finish('index')
    seen.append(tracker.overall())
    assert seen[0] == pytest.approx(0.1) and seen[1] == pytest.approx(0.5) and seen[2] == seen[1]
    assert seen[3] == 0.99
    tracker.complete()
    assert tracker.overall() == 1.0 and store.get('j')['overall'] == 1.0


def test_job_writes_are_throttled_to_two_per_second(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save({'id': 'j', 'status': 'ingesting'})
    now = [1000.0]
    monkeypatch.setattr(progress, 'clock', lambda: now[0])
    writes = []
    original = store.save
    monkeypatch.setattr(store, 'save', lambda job: (writes.append(job), original(job))[1])
    tracker = progress.Tracker.open(store, 'j')
    tracker.plan(['download', 'preview'])
    tracker.start('download', 'go')
    writes.clear()
    for step in range(400):                       # four seconds of 100 events per second
        now[0] += 0.01
        tracker.progress('download', step / 400, f'{step} bytes')
    assert 6 <= len(writes) <= 8                  # at most two per second, the last reading is never older than half a second
    tracker.finish('download')
    assert store.get('j')['stages'][0]['status'] == 'done' and writes[-1]['stages'][0]['fraction'] == 1.0


def test_stall_after_sixty_seconds_without_progress_names_the_reason(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save({'id': 'j', 'status': 'ingesting'})
    now = [5000.0]
    monkeypatch.setattr(progress, 'clock', lambda: now[0])
    tracker = progress.Tracker.open(store, 'j')
    tracker.plan(['download', 'transcribe'])
    tracker.start('download', 'go')
    tracker.start('transcribe', 'Preparing audio…', stall_after=300)
    now[0] += 59
    assert not any(v['stalled'] for v in progress.stage_views(tracker.stages, tracker))
    now[0] += 2
    views = {v['id']: v for v in progress.stage_views(tracker.stages, tracker)}
    assert views['download']['stalled'] and 'connection may have dropped' in views['download']['stall_reason']
    assert not views['transcribe']['stalled']      # given a longer allowance for a long Whisper section
    tracker.progress('download', 0.5, 'again', hint='The site reported a download error; yt-dlp is retrying.')
    assert not progress.stage_views(tracker.stages, tracker)[0]['stalled']
    now[0] += 61
    stalled = progress.stage_views(tracker.stages, tracker)[0]
    assert stalled['stalled'] and stalled['stall_reason'].startswith('The site reported a download error')
    tracker.heartbeat('transcribe', 'text only')   # a heartbeat refreshes text but is not progress
    assert progress.stage_views(tracker.stages, tracker)[1]['updated_at'] is not None
    now[0] += 250
    assert progress.stage_views(tracker.stages, tracker)[1]['stalled']


def test_join_fires_for_the_last_branch_only(tmp_path):
    store = Store(tmp_path)
    store.save({'id': 'j', 'status': 'queued'})
    tracker = progress.Tracker.open(store, 'j')
    tracker.plan(['preview', 'transcribe'], required={'transcript', 'preview'})
    assert tracker.mark_done('preview') is False
    assert tracker.mark_done('transcript') is True
    assert tracker.mark_done('transcript') is False


# -- lanes and queue positions --------------------------------------------------------------

def test_lanes_bound_concurrency_and_report_queue_position(tmp_path):
    store = Store(tmp_path)
    lane = intake.Lane('network', 2)
    release, started = threading.Event(), []
    try:
        for index in range(4):
            store.save({'id': f'j{index}', 'status': 'ingesting'})
            tracker = progress.Tracker.open(store, f'j{index}')
            tracker.plan(['download', 'transcribe'])
        progress.Tracker.open(store, 'j0').start('download')
        progress.Tracker.open(store, 'j0').progress('download', 0.4, 'x', eta=480)
        progress.Tracker.open(store, 'j1').start('transcribe')
        progress.Tracker.open(store, 'j1').progress('transcribe', 0.1, 'y', eta=1500)
        for index in range(4):
            lane.submit(f'j{index}', 'fetch', lambda i=index: (started.append(i), release.wait(10)))
        wait_for(lambda: len(started) == 2, what='two workers busy')
        time.sleep(0.1)
        assert sorted(started) == [0, 1]                      # the other two wait: at most two at once
        original, intake.NETWORK = intake.NETWORK, lane
        try:
            assert intake.queue_info('j0') is None and intake.queue_info('j1') is None
            third = intake.queue_info('j2')
            fourth = intake.queue_info('j3')
        finally:
            intake.NETWORK = original
        assert third['ahead'] == 2 and third['position'] == 3
        assert third['message'] == 'Waiting · 2 videos ahead (downloading, ~8 min left)'
        assert fourth['ahead'] == 3 and fourth['message'].startswith('Waiting · 3 videos ahead')
    finally:
        release.set()
        lane.shutdown()


def test_queued_job_message_names_the_video_ahead(tmp_path):
    store = Store(tmp_path)
    lane = intake.Lane('local', 1)
    release = threading.Event()
    try:
        store.save({'id': 'busy', 'status': 'transcribing'})
        store.save({'id': 'next', 'status': 'queued'})
        busy = progress.Tracker.open(store, 'busy')
        busy.plan(['transcribe']); busy.start('transcribe'); busy.progress('transcribe', 0.5, '', eta=480)
        lane.submit('busy', 'transcribe', release.wait, 10)
        lane.submit('next', 'preview', lambda: None)
        original, intake.LOCAL = intake.LOCAL, lane
        try:
            info = intake.queue_info('next')
            view = intake.progress_view({**store.get('next'), 'status': 'queued'})
        finally:
            intake.LOCAL = original
        assert info['message'] == 'Waiting · 1 video ahead (transcribing, ~8 min left)'
        assert view['message'] is not None
    finally:
        release.set()
        lane.shutdown()


# -- the whole pipeline ---------------------------------------------------------------------------

@needs_ffmpeg
def test_audio_first_transcribes_while_the_video_is_still_downloading(client, fake, monkeypatch):
    events = fake.log
    fake.gates['video'].clear()
    monkeypatch.setattr(media, 'transcribe', fake_transcribe(events))
    source_id = add_url(client)
    wait_for(lambda: ('end', 'audio') in events and ('transcribe-end',) in events, what='transcription during download')
    assert ('end', 'video') not in events                      # the video has not landed yet
    wait_for(lambda: stage(source(client, source_id), 'transcribe')['status'] == 'done', what='transcribe stage')
    mid = source(client, source_id)
    assert mid['status'] in {'ingesting', 'transcribing'} and stage(mid, 'transcribe')['status'] == 'done'
    assert stage(mid, 'download')['status'] == 'active' and mid['title'] == 'A long talk'
    assert mid['thumbnail_url'] == 'https://img.example/t.jpg' and mid['duration'] == 6.0
    fake.gates['video'].set()
    done = wait_for(lambda: (s := source(client, source_id))['status'] == 'ready' and stage(s, 'index')['status'] == 'done' and s,
                    what='ready source with every step recorded')
    assert [e for e in events if e[0] in {'start', 'end'}] == [('start', 'audio'), ('end', 'audio'), ('start', 'video'), ('end', 'video')]
    assert events.index(('transcribe-start', '.m4a')) < events.index(('end', 'video'))
    assert [s['status'] for s in done['stages']] == ['done', 'done', 'skipped', 'done', 'done']
    assert done['progress'] == 1.0 and stage(done, 'preview')['detail'] == 'Already plays in the browser'
    job = module.STORE.get(source_id)
    assert job['words'] and media.has_audio_stream(Path(job['source_path'])) and Path(job['preview_path']).is_file()
    assert job['source_path'].endswith('source.mp4') and job['clips']
    assert fake.options[0]['format'].startswith('bestaudio') and fake.options[1]['format'].startswith('bestvideo')
    assert all(o['continuedl'] for o in fake.options)


@needs_ffmpeg
@pytest.mark.parametrize(('quality', 'height'), [('small', 480), ('high', 2160)])
def test_range_and_quality_reach_yt_dlp_and_are_recorded_on_the_job(client, fake, monkeypatch, quality, height):
    monkeypatch.setattr(media, 'transcribe', fake_transcribe(fake.log))
    fake.info['duration'] = 600.0
    source_id = add_url(client, quality=quality, range={'start': 10, 'end': 70})
    wait_for(lambda: source(client, source_id)['status'] == 'ready', what='ready')
    listed = source(client, source_id)
    assert listed['range'] == {'start': 10.0, 'end': 70.0} and listed['quality'] == quality
    assert f'height<={height}' in fake.options[1]['format']
    assert all(o['force_keyframes_at_cuts'] and 'download_ranges' in o for o in fake.options)


@needs_ffmpeg
def test_range_past_the_end_of_the_video_fails_clearly_and_long_ends_are_clamped(client, fake, monkeypatch):
    source_id = add_url(client, range={'start': 7200, 'end': 7300})
    failed = wait_for(lambda: (s := source(client, source_id))['status'] == 'failed' and s, what='failed range')
    assert 'only 0:06 long' in failed['error'] and not fake.options
    monkeypatch.setattr(media, 'transcribe', fake_transcribe(fake.log))
    ok = add_url(client, range={'start': 1, 'end': 9999})
    wait_for(lambda: source(client, ok)['status'] == 'ready', what='ready')
    assert module.STORE.get(ok)['expected_duration'] == 5.0


def test_source_request_rejects_a_bad_range_and_range_on_files(client, tmp_path):
    for body in ({'url': URL, 'range': {'start': 50, 'end': 10}}, {'url': URL, 'range': {'start': -1, 'end': 10}},
                 {'url': URL, 'quality': 'huge'}, {'path': str(tmp_path / 'x.mp4'), 'range': {'start': 0, 'end': 5}}):
        assert client.post(f'{API}/sources', json=body).status_code == 422, body


@needs_ffmpeg
def test_site_without_a_separate_audio_stream_falls_back_to_one_pass(client, fake, monkeypatch):
    fake.fail['audio'] = 'Requested format is not available'
    monkeypatch.setattr(media, 'transcribe', fake_transcribe(fake.log))
    source_id = add_url(client)
    done = wait_for(lambda: (s := source(client, source_id))['status'] == 'ready' and s, what='ready')
    assert [e[1] for e in fake.log if e[0] == 'start'] == ['audio', 'source']
    assert stage(done, 'merge')['status'] == 'skipped' and stage(done, 'transcribe')['status'] == 'done'


@needs_ffmpeg
def test_merge_postprocessor_hook_drives_the_merge_stage(client, fake, monkeypatch):
    fake.fail['audio'] = 'no audio-only format'
    seen = {}
    original = fake.download

    def with_merge(opts):
        for hook in opts['postprocessor_hooks']:
            hook({'status': 'started', 'postprocessor': 'Merger'})
            seen['merge'] = 'started'
            hook({'status': 'finished', 'postprocessor': 'Merger'})
        return original(opts)
    fake.download = with_merge
    monkeypatch.setattr(media, 'transcribe', fake_transcribe(fake.log))
    source_id = add_url(client)
    done = wait_for(lambda: (s := source(client, source_id))['status'] == 'ready' and s, what='ready')
    assert seen['merge'] == 'started' and stage(done, 'merge')['status'] == 'done'


@needs_ffmpeg
def test_failed_download_marks_the_job_failed_with_a_retryable_stage(client, fake, monkeypatch):
    fake.fail['audio'] = 'boom'
    fake.fail['source'] = 'HTTP Error 403: Forbidden'
    source_id = add_url(client)
    failed = wait_for(lambda: (s := source(client, source_id))['status'] == 'failed' and s, what='failed')
    assert 'HTTP Error 403' in failed['error'] and failed['retryable'] and not failed['cancellable']
    assert stage(failed, 'download')['status'] == 'failed' and 'HTTP Error 403' in stage(failed, 'download')['detail']


# -- cancel ---------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_cancel_during_download_stops_yt_dlp_and_leaves_nothing_ready(client, fake, monkeypatch):
    fake.gates['audio'].clear()
    source_id = add_url(client)
    wait_for(lambda: stage(source(client, source_id), 'download')['fraction'], what='download progress')
    cancelled = client.post(f'{API}/sources/{source_id}/cancel')
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
    wait_for(lambda: not intake.NETWORK.snapshot(), what='the download thread to stop')
    listed = source(client, source_id)
    assert listed['status'] == 'cancelled' and listed['retryable'] and not listed['cancellable']
    assert all(s['status'] in {'skipped', 'done'} for s in listed['stages']) and listed['progress'] < 0.5
    job = module.STORE.get(source_id)
    assert job['status'] == 'cancelled' and not job.get('preview_path') and not job.get('words')
    assert ('end', 'audio') not in fake.log and list(module.STORE.directory(source_id).glob('audio*.part'))   # partial kept for resume
    assert client.post(f'{API}/sources/{source_id}/cancel').status_code == 409
    with pytest.raises(module.HTTPException):
        module.sources_for([source_id], require_speech=False)


@needs_ffmpeg
def test_cancel_during_ffmpeg_kills_the_process_and_keeps_no_preview(client, tmp_path, monkeypatch):
    source = tmp_path / 'long.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=30:duration=240', '-c:v', 'mpeg4', '-q:v', '5', str(source))
    started = []
    original = subprocess.Popen

    def spy(*args, **kwargs):
        proc = original(*args, **kwargs)
        started.append(proc)
        return proc
    monkeypatch.setattr(media.subprocess, 'Popen', spy)
    response = client.post('/api/jobs', json={'source': str(source), 'transcriber': 'none'})
    job_id = response.json()['id']
    wait_for(lambda: (stage_of(job_id, 'preview') or {}).get('fraction'), what='preview progress')
    assert stage_of(job_id, 'preview')['detail'].count(' of ') == 1
    cancelled = client.post(f'{API}/sources/{job_id}/cancel')
    assert cancelled.status_code == 200
    wait_for(lambda: started and started[0].poll() is not None, what='FFmpeg to be killed')
    wait_for(lambda: not intake.LOCAL.snapshot(), what='the preview thread to stop')
    directory = module.STORE.directory(job_id)
    assert not (directory / 'preview.mp4').exists() and not (directory / 'preview.partial.mp4').exists()
    assert module.STORE.get(job_id)['status'] == 'cancelled'


def stage_of(job_id, stage_id):
    job = module.STORE.get(job_id)
    return next((s for s in job.get('stages') or [] if s['id'] == stage_id), None)


@needs_ffmpeg
def test_run_ffmpeg_reports_seconds_and_stops_when_cancelled(tmp_path, monkeypatch):
    seen, stop = [], threading.Event()
    with pytest.raises(media.Cancelled):
        media.run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=30:duration=600', '-c:v', 'libx264', '-preset', 'veryfast',
                          str(tmp_path / 'out.mp4')], timeout=60, what='test encode',
                         on_progress=lambda seconds, speed: (seen.append(seconds), stop.set() if len(seen) > 2 else None),
                         cancelled=stop.is_set)
    assert seen and seen == sorted(seen)
    with pytest.raises(RuntimeError, match='failed'):
        media.run_ffmpeg(['-i', str(tmp_path / 'missing.mp4'), str(tmp_path / 'x.mp4')], timeout=30, what='bad input')


# -- retry, recovery and resume --------------------------------------------------------------------------------

@needs_ffmpeg
def test_retry_resumes_the_partial_file_and_reuses_finished_work(client, fake, monkeypatch):
    fake.fail['video'] = 'The connection dropped'
    monkeypatch.setattr(media, 'transcribe', fake_transcribe(fake.log))
    source_id = add_url(client)
    failed = wait_for(lambda: (s := source(client, source_id))['status'] == 'failed' and s, what='failed download')
    assert stage(failed, 'transcribe')['status'] in {'done', 'active', 'pending', 'skipped'}
    wait_for(lambda: not intake.LOCAL.snapshot() and not intake.NETWORK.snapshot(), what='lanes to drain')
    directory = module.STORE.directory(source_id)
    (directory / 'video.mp4.part').write_bytes(b'partial video bytes')
    fake.fail.clear()
    fake.log.clear()
    response = client.post(f'{API}/sources/{source_id}/retry')
    assert response.status_code == 202 and response.json()['status'] in {'queued', 'ingesting', 'transcribing'}
    wait_for(lambda: source(client, source_id)['status'] == 'ready', what='retry to finish')
    assert (directory / 'video.mp4.part').exists() is False        # yt-dlp finished the same file
    assert all(o['continuedl'] and o['outtmpl'] == str(directory / (Path(o['outtmpl']).name)) for o in fake.options)
    assert client.post(f'{API}/sources/{source_id}/retry').status_code == 409


def test_retry_only_applies_to_failed_or_cancelled_sources(client):
    module.STORE.save({'id': 'ready1', 'status': 'ready', 'source': URL, 'title': 't'})
    assert client.post(f'{API}/sources/ready1/retry').status_code == 409
    assert client.post(f'{API}/sources/missing/retry').status_code == 404
    assert client.post(f'{API}/sources/{"0" * 32}/cancel').status_code == 404


def test_restart_marks_in_flight_jobs_failed_and_retryable(tmp_path, monkeypatch):
    store = Store(tmp_path / 'data')
    store.save({'id': 'a' * 32, 'status': 'ingesting', 'source': URL, 'title': 't', 'progress': 'x', 'options': {},
                'request': {'transcriber': 'auto'}, 'stages': [
                    {'id': 'download', 'label': 'Download', 'status': 'active', 'detail': '1 of 2 GB', 'fraction': .5},
                    {'id': 'transcribe', 'label': 'Transcription', 'status': 'pending'}]})
    store.save({'id': 'b' * 32, 'status': 'queued', 'source': URL, 'title': 'q'})
    store.save({'id': 'c' * 32, 'status': 'ready', 'source': URL, 'title': 'r'})
    (store.directory('a' * 32) / 'video.mp4.part').write_bytes(b'0123')
    monkeypatch.setattr(module, 'STORE', store)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    with TestClient(module.app) as client:
        listed = {s['id']: s for s in client.get(f'{API}/sources').json()}
        assert listed['a' * 32]['status'] == 'failed' and listed['a' * 32]['retryable']
        assert 'Retry to continue' in listed['a' * 32]['error']
        assert stage(listed['a' * 32], 'download')['status'] == 'failed' and 'Interrupted' in stage(listed['a' * 32], 'download')['detail']
        assert listed['b' * 32]['status'] == 'failed' and listed['c' * 32]['status'] == 'ready'
    assert (store.directory('a' * 32) / 'video.mp4.part').read_bytes() == b'0123'     # the partial survives for yt-dlp to resume


# -- pre-flight --------------------------------------------------------------------------------------------------

def test_inspect_reports_title_duration_options_and_estimates(client, fake, monkeypatch):
    fake.info['duration'] = 9300.0
    response = client.post(f'{API}/sources/inspect', json={'url': URL})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['title'] == 'A long talk' and body['duration'] == 9300.0 and body['uploader'] == 'Channel'
    assert body['thumbnail_url'] == 'https://img.example/t.jpg'
    assert [o['id'] for o in body['options']] == ['standard', 'high', 'small', 'audio_first']
    assert body['options'][0]['label'] == '720p · 30 fps'
    assert body['estimates']['download_bytes'] == {'standard': 10_000_000, 'high': 10_000_000, 'small': 10_000_000, 'audio_first': 1_000_000}
    assert body['estimates']['transcribe_seconds'] == pytest.approx(9300 * intake.DEFAULT_RTF[media.resolve_provider('auto')])
    assert body['estimates']['download_seconds'] == {'standard': None, 'high': None, 'small': None, 'audio_first': None}


def test_inspect_uses_measured_speeds_and_rejects_bad_links(client, fake, tmp_path):
    stats = progress.Stats(module.STORE.root)
    stats.record('download_bytes_per_second', 2_000_000)
    stats.record(f'transcribe_rtf.{media.resolve_provider("auto")}', 0.1)
    body = client.post(f'{API}/sources/inspect', json={'url': URL}).json()
    assert body['estimates']['download_seconds']['standard'] == 5 and body['estimates']['transcribe_seconds'] == pytest.approx(0.6)
    assert client.post(f'{API}/sources/inspect', json={'url': 'ftp://x'}).status_code == 422
    assert client.post(f'{API}/sources/inspect', json={'url': '/etc/passwd'}).status_code == 422
    assert client.post(f'{API}/sources/inspect', json={}).status_code == 422


def test_inspect_is_bounded_in_time_and_reports_unreadable_links(client, fake, monkeypatch):
    monkeypatch.setattr(download, 'INSPECT_TIMEOUT', 0.2)
    release = threading.Event()
    fake.info = None
    original = fake.YoutubeDL.extract_info
    monkeypatch.setattr(fake.YoutubeDL, 'extract_info', lambda self, *a, **k: (release.wait(5), {})[1])
    slow = client.post(f'{API}/sources/inspect', json={'url': URL})
    release.set()
    assert slow.status_code == 422 and 'took longer than' in slow.json()['detail']
    monkeypatch.setattr(fake.YoutubeDL, 'extract_info', original)

    def unreadable(self, *a, **k):
        raise RuntimeError('Unsupported URL: https://video.example/x')
    monkeypatch.setattr(fake.YoutubeDL, 'extract_info', unreadable)
    monkeypatch.setattr(download, 'INSPECT_TIMEOUT', 5)
    assert 'Unsupported URL' in client.post(f'{API}/sources/inspect', json={'url': URL}).json()['detail']


# -- transcription in chunks ----------------------------------------------------------------------------------------

@needs_ffmpeg
def test_mlx_transcription_runs_in_chunks_with_rebased_timestamps_and_progress(tmp_path, monkeypatch):
    audio = tmp_path / 'talk.m4a'
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=300:duration=33', '-c:a', 'aac', str(audio))
    monkeypatch.setattr(media, 'MLX_CHUNK_SECONDS', 10)
    monkeypatch.setattr(media, 'resolve_provider', lambda provider: 'mlx')
    calls = []

    def mlx_transcribe(path, **kwargs):
        calls.append(Path(path).name)
        return {'segments': [{'words': [{'word': ' hello', 'start': 1.0, 'end': 1.4}, {'word': ' there', 'start': 4.0, 'end': 4.3}]}]}
    monkeypatch.setitem(__import__('sys').modules, 'mlx_whisper', types.SimpleNamespace(transcribe=mlx_transcribe))
    seen = []
    hooks = media.TranscribeHooks(started=lambda a, b: seen.append(('start', a, b)),
                                  finished=lambda covered, total, words: seen.append(('done', covered, total, len(words))))
    result = media.transcribe(audio, tmp_path, provider='mlx', hooks=hooks)
    assert len(calls) == 4                                   # 0-10, 8-18, 16-26, 24-33
    starts = [round(w['start'], 1) for w in result['words']]
    assert starts == sorted(starts) and starts[:4] == [1.0, 4.0, 9.0, 12.0]   # chunk 2 owns speech after the 1 s seam at 9 s
    assert [e for e in seen if e[0] == 'done'] == [('done', 10, 33.0, 2), ('done', 18, 33.0, 4), ('done', 26, 33.0, 6), ('done', 33.0, 33.0, 8)]
    assert [e[1] for e in seen if e[0] == 'start'] == [0.0, 8.0, 16.0, 24.0]
    assert [w['id'] for w in result['words']] == list(range(len(result['words'])))
    assert not (tmp_path / 'transcript-chunks').exists()


@needs_ffmpeg
def test_finished_chunks_are_reused_after_an_interruption(tmp_path, monkeypatch):
    audio = tmp_path / 'talk.m4a'
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=300:duration=25', '-c:a', 'aac', str(audio))
    monkeypatch.setattr(media, 'MLX_CHUNK_SECONDS', 10)
    calls = []

    def mlx_transcribe(path, **kwargs):
        calls.append(Path(path).name)
        if len(calls) == 3:
            raise RuntimeError('out of memory')
        return {'segments': [{'words': [{'word': ' hi', 'start': 1.0, 'end': 1.4}]}]}
    monkeypatch.setitem(__import__('sys').modules, 'mlx_whisper', types.SimpleNamespace(transcribe=mlx_transcribe))
    with pytest.raises(RuntimeError, match='out of memory'):
        media.transcribe(audio, tmp_path, provider='mlx')
    assert len(calls) == 3
    calls.clear()
    result = media.transcribe(audio, tmp_path, provider='mlx')
    assert len(calls) == 1 and len(result['words']) == 3        # only the unfinished chunk ran again


@needs_ffmpeg
def test_cancel_between_chunks_stops_transcription(tmp_path, monkeypatch):
    audio = tmp_path / 'talk.m4a'
    ffmpeg('-f', 'lavfi', '-i', 'sine=frequency=300:duration=25', '-c:a', 'aac', str(audio))
    monkeypatch.setattr(media, 'MLX_CHUNK_SECONDS', 10)
    cancelled, calls = threading.Event(), []

    def mlx_transcribe(path, **kwargs):
        calls.append(path)
        cancelled.set()
        return {'segments': [{'words': [{'word': ' hi', 'start': 1.0, 'end': 1.4}]}]}
    monkeypatch.setitem(__import__('sys').modules, 'mlx_whisper', types.SimpleNamespace(transcribe=mlx_transcribe))
    with pytest.raises(media.Cancelled):
        media.transcribe(audio, tmp_path, provider='mlx', hooks=media.TranscribeHooks(cancelled=cancelled.is_set))
    assert len(calls) == 1


def test_transcribe_meter_estimates_from_the_measured_realtime_factor(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save({'id': 'j', 'status': 'transcribing'})
    env = intake.Env(store=store, keys={'groq': ''}, select=None, clean=str)
    progress.Stats(tmp_path).record('transcribe_rtf.mlx', 0.05)
    tracker = progress.Tracker.open(store, 'j')
    tracker.plan(['transcribe'])
    tracker.start('transcribe')
    meter = intake.TranscribeMeter(env, tracker, 'mlx', 9300.0, tmp_path)
    assert meter.eta() == pytest.approx(465.0)                  # 2 h 35 m of audio at 0.05 s per audio second
    meter.span, meter.began = (0.0, 600.0), time.monotonic() - 30
    meter.finished(600.0, 9300.0, [{'id': 0, 'text': 'hi', 'start': 1, 'end': 2}])
    tracker.flush(force=True)
    saved = store.get('j')['stages'][0]
    assert saved['fraction'] == pytest.approx(600 / 9300, abs=1e-3) and 'of 2:35:00' in saved['detail'] and 'min left' in saved['detail']
    assert json.loads((tmp_path / 'transcript.partial.json').read_text())['covered_seconds'] == 600.0
    assert meter.rtf == pytest.approx(30 / 600, rel=0.05)       # the new measurement replaces the stored average
    meter.close()
    assert progress.Stats(tmp_path).get('transcribe_rtf.mlx') == pytest.approx((0.05 + 0.05) / 2, rel=0.2)


# -- activity -------------------------------------------------------------------------------------------------------------------

def test_activity_lists_running_sources_and_attention_items(client):
    module.STORE.save({'id': 'r' * 32, 'status': 'ingesting', 'title': 'Running', 'source': URL, 'progress': 'x', 'overall': .3,
                       'stages': [{'id': 'download', 'label': 'Download', 'status': 'active', 'fraction': .3, 'detail': '1 of 3 GB',
                                   'progress_at': time.time()}]})
    module.STORE.save({'id': 'f' * 32, 'status': 'failed', 'title': 'Broken', 'source': URL, 'error': 'HTTP 403', 'words': []})
    module.STORE.save({'id': 'd' * 32, 'status': 'ready', 'title': 'Done', 'source': URL, 'words': [{}]})
    body = client.get(f'{API}/activity').json()
    by_id = {item['id']: item for item in body['items']}
    assert set(by_id) == {'r' * 32, 'f' * 32}
    assert by_id['r' * 32]['state'] == 'running' and by_id['r' * 32]['actions'] == [{'id': 'cancel', 'label': 'Cancel'}]
    assert by_id['f' * 32]['state'] == 'attention' and by_id['f' * 32]['actions'] == [{'id': 'retry', 'label': 'Retry'}]
    assert body['running'] == 1 and body['attention'] == 1


def test_restored_stream_requires_original_and_downloaded_identity():
    job = {'source_freed_at': '2026-01-01', 'freed_provider_id': 'same', 'provenance': {'id': 'same'}}
    env = types.SimpleNamespace(store=types.SimpleNamespace(get=lambda _: job))
    tracker = types.SimpleNamespace(job_id='job')
    intake.validate_remote_identity(env, tracker, {'id': 'same'})
    for original, inspected, downloaded in [(None, 'same', 'same'), ('same', None, 'same'), ('same', 'same', None), ('same', 'same', 'other')]:
        job.update(freed_provider_id=original, provenance={'id': inspected})
        with pytest.raises(ValueError, match='identity could not be verified'):
            intake.validate_remote_identity(env, tracker, {'id': downloaded})
