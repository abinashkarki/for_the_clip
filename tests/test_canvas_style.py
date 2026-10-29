"""Bounded same-source canvas composition and unchanged foreground/audio evidence."""
import json
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest
from pydantic import ValidationError

from clippie import render, planner, collection_preferences
from clippie.collection_api import Constraints, SettingsRequest


def frame(path):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '0.3', '-i', str(path), '-frames:v', '1',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(640, 360, 3)


def audio(path):
    return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-f', 's16le',
                           '-acodec', 'pcm_s16le', 'pipe:1'], capture_output=True, check=True).stdout


def test_blurred_canvas_preserves_sharp_corners_real_black_and_original_audio(tmp_path):
    image = Image.new('RGB', (320, 180), 'red')
    draw = ImageDraw.Draw(image)
    draw.rectangle((160, 0, 319, 89), fill='lime')
    draw.rectangle((0, 90, 159, 179), fill='blue')
    draw.rectangle((160, 90, 319, 179), fill='yellow')
    draw.rectangle((130, 65, 190, 115), fill='black')
    png, source = tmp_path / 'corners.png', tmp_path / 'source.mp4'
    image.save(png)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-loop', '1', '-i', str(png), '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000', '-t', '1.2', '-r', '30', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True, capture_output=True)
    sources = {'s': {'source_path': source, 'duration': 1.2, 'words': []}}
    base = {'schema_version': 2, 'width': 360, 'height': 640, 'captions': False,
            'audio': {'normalize': False}, 'clips': [{'id': 'c', 'source_id': 's', 'start': 0, 'end': 1,
                                                    'framing': {'mode': 'fit'}}]}
    outputs, measurements = {}, {}
    for style in ['plain', 'blurred']:
        started = time.monotonic()
        outputs[style] = render.render_production(dict(base, canvas_style=style), sources, tmp_path / style)
        measurements[style] = {'wall_seconds': time.monotonic() - started,
                               'render_metrics': outputs[style]['metrics']}
    plain, blurred = [frame(outputs[style]['video_path']) for style in ['plain', 'blurred']]
    assert plain[:180].mean() < 3
    assert blurred[:180].mean() > 20
    # Source corners survive the complete-image fit at identical positions.
    for y, x, channel in [(230, 15, 0), (230, 345, 1), (410, 15, 2), (410, 345, 0)]:
        assert blurred[y, x, channel] > 200
    assert np.abs(plain[225:414].astype(float) - blurred[225:414].astype(float)).mean() < 4
    assert blurred[310:330, 170:190].mean() < 5  # Real black foreground is opaque, never keyed out.
    seam = blurred[40, 180]
    assert seam[0] > 30 and seam[1] > 30  # Soft mixed transition, unlike a sharp corner boundary.
    assert audio(outputs['plain']['video_path']) == audio(outputs['blurred']['video_path'])
    (tmp_path / 'paired-measurement.json').write_text(json.dumps(measurements, indent=2))
    print('CANVAS_PAIRED_MEASUREMENT=' + json.dumps(measurements))


def test_canvas_validation_and_legacy_omission():
    sources = {'s': {'duration': 2}}
    clips = [{'id': 'one', 'source_id': 's', 'start': 0, 'end': 1, 'role': 'hook'},
             {'id': 'two', 'source_id': 's', 'start': 1, 'end': 2, 'role': 'payoff'}]
    legacy = planner.validate_timeline({'clips': clips}, sources)
    assert 'canvas_style' not in legacy
    assert planner.validate_timeline({'schema_version': 2, 'clips': clips}, sources)['canvas_style'] == 'plain'
    for bad in ['automatic', True, None, 1]:
        with pytest.raises(ValueError):
            planner.validate_timeline({'schema_version': 2, 'clips': clips, 'canvas_style': bad}, sources)
        with pytest.raises(ValidationError):
            Constraints(canvas_style=bad)
        with pytest.raises(ValidationError):
            SettingsRequest(base_revision='r', request_id='q', canvas_style=bad)
    assert Constraints().canvas_style == 'plain'
    assert collection_preferences.DEFAULTS['canvas_style'] == 'plain'
