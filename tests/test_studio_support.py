"""Shared Studio API fixtures: stand-in engine modules faithful to docs/v3 signatures, and a seeded app.

The stand-ins enforce a few key timeline rules and map words through segments and cuts exactly as
TIMELINE_V3.md describes, so the same tests are meant to pass against the real modules.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass
import re
import subprocess
import sys
import time
import types

from fastapi.testclient import TestClient
import pytest

from clippie import app as module
from clippie.store import Store

WORDS = [(0, 'So', 0.0, 0.5), (1, 'how', 0.5, 1.0), (2, 'um', 1.0, 1.4), (3, 'would', 1.5, 2.0),
         (4, 'you', 2.0, 2.5), (5, 'approach', 2.5, 3.2), (6, 'this', 4.5, 5.0), (7, 'uh', 5.0, 5.4),
         (8, 'problem', 5.5, 6.2), (9, 'today', 6.2, 6.8), (10, 'honestly', 6.8, 7.5), (11, 'yes', 7.5, 8.0)]
RATIOS = {(9, 16), (16, 9), (1, 1), (4, 5)}
TEMPLATES = {'bold_pop': {'id': 'bold_pop', 'name': 'Bold pop'}, 'boxed_word': {'id': 'boxed_word', 'name': 'Boxed word'},
             'color_word': {'id': 'color_word', 'name': 'Color word'}, 'phrase_clean': {'id': 'phrase_clean', 'name': 'Phrase'}}
HOOKS = {'hook_box': {'id': 'hook_box'}, 'hook_outline': {'id': 'hook_outline'}}
V2_STYLES = {'clean_focus': 'boxed_word', 'editorial': 'color_word', 'phrase': 'phrase_clean'}


class FakeTimelineError(ValueError):
    def __init__(self, message, path=''):
        super().__init__(message)
        self.path = path


def _fail(path, message):
    raise FakeTimelineError(message, path)


def _kept(segment):
    position, ranges = segment['start'], []
    for cut in segment.get('cuts') or []:
        ranges.append((position, cut['start']))
        position = cut['end']
    ranges.append((position, segment['end']))
    return [(a, b) for a, b in ranges if b > a]


def fake_duration(timeline):
    return round(sum(b - a for s in timeline['segments'] for a, b in _kept(s)), 3)


def fake_validate(timeline, sources):
    result = {key: copy.deepcopy(timeline.get(key)) for key in
              ('schema_version', 'canvas', 'segments', 'shots', 'broll', 'captions', 'overlays', 'brand_kit_id', 'audio')
              if key in timeline}
    if result.get('schema_version') != 3:
        _fail('schema_version', 'must be 3')
    canvas = result.setdefault('canvas', {'width': 1080, 'height': 1920, 'fps': 30, 'background': {'kind': 'blur', 'color': '#000000'}})
    width, height = canvas['width'], canvas['height']
    if width % 2 or height % 2 or not (240 <= width <= 3840 and 240 <= height <= 3840):
        _fail('canvas', 'width/height must be even integers 240-3840')
    from math import gcd
    if (width // gcd(width, height), height // gcd(width, height)) not in RATIOS:
        _fail('canvas', 'aspect must be 9:16, 16:9, 1:1 or 4:5')
    if canvas.get('fps') not in (24, 25, 30):
        _fail('canvas.fps', 'fps must be 24, 25 or 30')
    segments = result.get('segments')
    if not isinstance(segments, list) or not segments:
        _fail('segments', 'at least one segment is required')
    if len({s['id'] for s in segments}) != len(segments):
        _fail('segments', 'segment ids must be unique')
    for index, segment in enumerate(segments):
        path = f'segments[{index}]'
        if segment['source_id'] not in sources:
            _fail(path, f'unknown source {segment["source_id"]}')
        if not 0 <= segment['start'] < segment['end'] <= sources[segment['source_id']]['duration']:
            _fail(path, 'need 0 <= start < end <= source duration')
        previous = segment['start']
        segment.setdefault('cuts', [])
        for number, cut in enumerate(segment['cuts']):
            if cut['reason'] not in {'filler', 'silence', 'manual'}:
                _fail(f'{path}.cuts[{number}]', 'unknown reason')
            if cut['end'] - cut['start'] < 0.04 - 1e-9:
                _fail(f'{path}.cuts[{number}]', 'cut shorter than 0.04 s')
            if not (cut['start'] > segment['start'] and cut['end'] < segment['end']) or cut['start'] < previous:
                _fail(f'{path}.cuts[{number}]', 'cuts must be sorted, non-overlapping and strictly inside the segment')
            previous = cut['end']
    total = fake_duration(result)
    if not 0.5 < total <= 180:
        _fail('segments', 'program duration must be > 0.5 s and <= 180 s')
    shots = result.get('shots') or []
    if not shots:
        frame = {'t': 0.0, 'x': 0.5, 'y': 0.5, 'zoom': 1.0}
        shots = [{'id': 'sh1', 'start': 0.0, 'end': total, 'layout': 'fit', 'regions': [{'keyframes': [frame]}]}]
    if abs(shots[0]['start']) > 1e-3 or abs(shots[-1]['end'] - total) > 1e-3:
        _fail('shots', 'shots must cover [0, duration]')
    for before, after in zip(shots, shots[1:]):
        if abs(before['end'] - after['start']) > 1e-3:
            _fail('shots', 'shots must be contiguous')
    shots[-1]['end'] = total
    result['shots'] = shots
    result.setdefault('broll', [])
    captions = result.setdefault('captions', {'enabled': True, 'template_id': 'bold_pop', 'overrides': {}, 'emphasis': [], 'hidden': []})
    if captions.get('template_id') not in TEMPLATES:
        _fail('captions.template_id', 'unknown template')
    words = {f'{sid}:{w["id"]}' for sid in {s['source_id'] for s in segments} for w in sources[sid].get('words', [])}
    for field in ('emphasis', 'hidden'):
        captions[field] = [key for key in captions.get(field) or [] if key in words]
    captions.setdefault('overrides', {})
    overlays = result.setdefault('overlays', [])
    if len({o['id'] for o in overlays}) != len(overlays) or sum(o['kind'] == 'progress_bar' for o in overlays) > 1:
        _fail('overlays', 'overlay ids must be unique and at most one progress bar')
    for index, overlay in enumerate(overlays):
        if overlay['kind'] == 'hook' and not (1 <= len(overlay['text']) <= 120 and 0 <= overlay['start'] < overlay['end'] <= total + 1e-3):
            _fail(f'overlays[{index}]', 'invalid hook')
        if overlay['kind'] == 'progress_bar' and not re.fullmatch(r'#[0-9A-Fa-f]{6}', overlay['color']):
            _fail(f'overlays[{index}]', 'invalid color')
        if overlay['kind'] not in {'hook', 'progress_bar', 'logo'}:
            _fail(f'overlays[{index}]', 'unknown overlay kind')
    result.setdefault('brand_kit_id', None)
    result.setdefault('audio', {'normalize': True})
    return result


def fake_program_words(timeline, sources):
    result, at = [], 0.0
    for segment in timeline['segments']:
        for start, end in _kept(segment):
            for word in sources[segment['source_id']].get('words', []):
                if start <= (word['start'] + word['end']) / 2 < end:
                    key = f'{segment["source_id"]}:{word["id"]}'
                    result.append({'key': key, 'text': word['text'], 'source_id': segment['source_id'],
                                   'word_id': word['id'], 'start': at + max(word['start'], start) - start,
                                   'end': at + min(word['end'], end) - start,
                                   'emphasis': key in timeline['captions']['emphasis'],
                                   'hidden': key in timeline['captions']['hidden']})
            at += end - start
    return result


def fake_from_v2(v2, sources):
    segments = [{'id': f's{i + 1}', 'source_id': clip['source_id'], 'start': float(clip['start']), 'end': float(clip['end']), 'cuts': []}
                for i, clip in enumerate(v2['clips'])]
    overlays = [{'id': f'o{i + 1}', 'kind': 'hook', 'text': title['text'], 'start': float(title['at']),
                 'end': float(title['at'] + title['duration']), 'style': 'hook_box'} for i, title in enumerate(v2.get('titles') or [])]
    timeline = {'schema_version': 3, 'segments': segments, 'overlays': overlays,
                'canvas': {'width': 1080, 'height': 1920, 'fps': 30,
                           'background': {'kind': 'blur' if v2.get('canvas_style') == 'blurred' else 'color', 'color': '#000000'}},
                'captions': {'enabled': v2.get('captions', True), 'template_id': V2_STYLES.get(v2.get('caption_style'), 'boxed_word'),
                             'overrides': {}, 'emphasis': [], 'hidden': []}}
    return fake_validate(timeline, sources)


def fake_fillers(words, *, language='en', silence_min=0.6, keep_padding=0.08):
    found = [{'start': w['start'], 'end': w['end'], 'reason': 'filler', 'word_ids': [w['id']]}
             for w in words if w['text'].lower().strip('.,') in {'um', 'uh', 'erm', 'ah', 'hmm'}]
    for before, after in zip(words, words[1:]):
        if after['start'] - before['end'] >= silence_min:
            found.append({'start': round(before['end'] + keep_padding, 3), 'end': round(after['start'] - keep_padding, 3),
                          'reason': 'silence', 'word_ids': []})
    return sorted(found, key=lambda item: item['start'])


def fake_auto_shots(timeline, sources, *, analysis=None, cache_dir=None):
    total = fake_duration(timeline)
    half = round(total / 2, 3)
    return [{'id': 'sh1', 'start': 0.0, 'end': half, 'layout': 'fill',
             'regions': [{'keyframes': [{'t': 0.0, 'x': 0.6, 'y': 0.4, 'zoom': 1.0}, {'t': half * 0.9, 'x': 0.62, 'y': 0.4, 'zoom': 1.0}]}]},
            {'id': 'sh2', 'start': half, 'end': total, 'layout': 'fit',
             'regions': [{'keyframes': [{'t': half, 'x': 0.5, 'y': 0.5, 'zoom': 1.0}]}]}]


@dataclass
class Engine:
    renders: list
    state: dict


def install_engine(monkeypatch):
    state = {'render_error': None, 'render_delay': 0.0, 'reframe_error': None, 'media_info': {}}
    renders = []
    # This stand-in models an HD original and probed encoded outputs. Real-file
    # resolution enforcement is exercised separately in test_final_quality.
    from clippie import final_quality
    monkeypatch.setattr(final_quality, 'probe', lambda path: state['media_info'].get(str(path),
        {'width': 1920, 'height': 1080, 'duration': 10.0, 'has_audio': True}))
    timeline = types.ModuleType('clippie.timeline_v3')
    timeline.TimelineError = FakeTimelineError
    timeline.validate, timeline.duration, timeline.program_words = fake_validate, fake_duration, fake_program_words
    timeline.from_v2 = fake_from_v2

    def render(timeline_value, sources, directory, *, assets=None, progress=None, preview=False, process_owner=None):
        renders.append({'timeline': timeline_value, 'directory': str(directory), 'assets': assets, 'preview': preview, 'sources': sources})
        if state['render_delay']:
            time.sleep(state['render_delay'])
        if state['render_error']:
            raise RuntimeError(state['render_error'])
        progress and progress(0.5)
        video, srt, manifest = directory / 'short.mp4', directory / 'short.srt', directory / 'manifest.json'
        video.write_bytes(b'\x00\x00\x00\x18ftypmp42' + bytes(range(256)) * 16)
        srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
        manifest.write_text('{}')
        progress and progress(1.0)
        width, height = (540, 960) if preview else (timeline_value['canvas']['width'], timeline_value['canvas']['height'])
        state['media_info'][str(video)] = {'width': width, 'height': height, 'duration': fake_duration(timeline_value), 'has_audio': True}
        return {'video_path': str(video), 'srt_path': str(srt), 'manifest_path': str(manifest),
                'duration': fake_duration(timeline_value), 'width': width, 'height': height, 'warnings': []}
    render_v3 = types.ModuleType('clippie.render_v3')
    render_v3.render = render
    captions = types.ModuleType('clippie.caption_templates')
    captions.load = lambda: {'captions': copy.deepcopy(TEMPLATES), 'hooks': copy.deepcopy(HOOKS)}

    def resolve(template_id, overrides):
        if template_id not in TEMPLATES:
            raise ValueError('unknown template')
        if set(overrides) - {'colors', 'case', 'stroke_width'}:
            raise ValueError('override key not allowed: ' + ', '.join(sorted(set(overrides) - {'colors', 'case', 'stroke_width'})))
        return {**TEMPLATES[template_id], **overrides}
    captions.resolve = resolve
    analyzers = types.ModuleType('clippie.analyzers')
    fillers = types.ModuleType('clippie.analyzers.fillers')
    fillers.detect = fake_fillers
    reframe = types.ModuleType('clippie.reframe')

    def auto_shots(*args, **kwargs):
        if state['reframe_error']:
            raise RuntimeError(state['reframe_error'])
        return fake_auto_shots(*args, **kwargs)
    reframe.auto_shots = auto_shots
    for name, value in {'clippie.timeline_v3': timeline, 'clippie.render_v3': render_v3, 'clippie.caption_templates': captions,
                        'clippie.analyzers': analyzers, 'clippie.analyzers.fillers': fillers, 'clippie.reframe': reframe}.items():
        monkeypatch.setitem(sys.modules, name, value)
    return Engine(renders, state)


@pytest.fixture
def fake_engine(monkeypatch):
    return install_engine(monkeypatch)


@pytest.fixture
def no_engine(monkeypatch):
    for name in ('timeline_v3', 'render_v3', 'caption_templates', 'reframe'):
        monkeypatch.setitem(sys.modules, f'clippie.{name}', None)
    monkeypatch.setitem(sys.modules, 'clippie.analyzers.fillers', None)


def make_video(path, seconds=10, size='320x180'):
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=blue:s={size}:r=30:d={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-shortest', str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope='session')
def video_bytes(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp('studio-media') / 'source.mp4').read_bytes()


def v2_timeline(start=0.0, end=8.0):
    return {'schema_version': 2, 'aspect_ratio': '9:16', 'caption_style': 'clean_focus', 'canvas_style': 'blurred',
            'captions': True, 'titles': [], 'clips': [{'id': 'c1', 'source_id': 'src', 'start': start, 'end': end}]}


@dataclass
class Studio:
    client: TestClient
    root: object
    media: object
    engine: Engine

    def clip(self, title='Khanmigo asks how you would approach it', timeline=None, document=None):
        """A project with one ready clip whose current revision is v2-only. Returns (project, short, revision)."""
        from clippie.collection_api import store
        collections = store()
        project = collections.create_collection(['src'], constraints={'source_roles': {'src': 'primary'}}, name='Demo project')
        short = collections.add_short(project['id'], metadata={})
        timeline = timeline or v2_timeline()
        revision = collections.append_revision(short['id'], document or {
            'plan': {'title': title, 'edit_format': 'explanation', 'timeline': timeline},
            'timeline': timeline, 'editorial': {'status': 'pass', 'issues': []}, 'technical': 'pass',
            'human_review': 'unreviewed', 'artifact_state': 'plan'}, expected_revision=None)
        return project, collections.get_short(short['id']), revision

    def wait(self, job_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get(f'/api/studio/jobs/{job_id}').json()
            if job['status'] in {'done', 'failed'}:
                return job
            time.sleep(0.02)
        pytest.fail('Studio job did not finish.')

    def detail(self, clip_id):
        response = self.client.get(f'/api/studio/clips/{clip_id}')
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def studio(tmp_path, monkeypatch, fake_engine, video_bytes):
    media_path = tmp_path / 'source.mp4'
    media_path.write_bytes(video_bytes)
    sources = Store(tmp_path / 'data')
    sources.save({'id': 'src', 'title': 'Sal Khan on Transforming Education', 'status': 'ready', 'progress': 'Ready',
                  'source_path': str(media_path), 'preview_path': str(media_path), 'duration': 10.0,
                  'source_info': {'width': 320, 'height': 180, 'duration': 10.0, 'has_audio': True},
                  'words': [{'id': i, 'text': text, 'start': a, 'end': b} for i, text, a, b in WORDS], 'segments': []})
    monkeypatch.setattr(module, 'STORE', sources)
    monkeypatch.setattr(module, 'WORKER', ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(module, 'KEYS', {'gemini': 'fake-test-key', 'deepseek': '', 'groq': ''})
    monkeypatch.setattr(module, 'process_job', lambda job_id, request: None)
    with TestClient(module.app, headers={'X-Clippie-Observation': 'qa'}) as client:
        yield Studio(client, tmp_path / 'data', media_path, fake_engine)
    module.WORKER.shutdown(wait=True)


def png_bytes():
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGBA', (16, 16), (255, 0, 0, 255)).save(buffer, 'PNG')
    return buffer.getvalue()
