"""Offline fixture probe executed by the app's Python, never the developer venv."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--whisper-model')
    parser.add_argument('--embedding-model')
    parser.add_argument('--speech-fixture')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'python': sys.version, 'executable': sys.executable, 'imports': {}, 'network_models': False}
    started = time.monotonic()
    resources = Path(os.environ['CLIPPIE_RESOURCE_DIR']).resolve().parents[1]
    for name in ('fastapi', 'uvicorn', 'numpy', 'PIL', 'mlx.core', 'mlx_whisper',
                 'torch', 'torchvision', 'sentence_transformers', 'transformers', 'cv2', 'keyring'):
        print(json.dumps({'stage': 'import', 'module': name}), flush=True)
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(resources):
            raise RuntimeError(f'{name} was imported from outside this app: {path}')
        report['imports'][name] = str(path.relative_to(resources))
    import mlx.core as mx
    print(json.dumps({'stage': 'mlx_compute'}), flush=True)
    value = mx.sum(mx.array([1.0, 2.0, 3.0]))
    mx.eval(value)
    if value.item() != 6.0:
        raise RuntimeError('MLX compute check failed.')
    report['mlx_compute'] = True
    import numba
    print(json.dumps({'stage': 'torch_numba_compute'}), flush=True)
    import numpy as np
    import torch
    import torchvision
    if not torchvision.extension._has_ops():
        raise RuntimeError('Bundled TorchVision native operators are unavailable.')
    torch.set_num_threads(2)
    if torch.sum(torch.tensor([1.0, 2.0, 3.0])).item() != 6.0:
        raise RuntimeError('Torch compute check failed.')
    numba.set_num_threads(2)
    @numba.njit(parallel=True, cache=False)
    def parallel_sum(values):
        total = 0.0
        for index in numba.prange(values.size):
            total += values[index]
        return total
    if parallel_sum(np.array([1.0, 2.0, 3.0])) != 6.0:
        raise RuntimeError('Numba/OpenMP compute check failed.')
    report['torch_numba_compute'] = {'passed': True, 'numba_threading_layer': numba.threading_layer()}
    from clippie import media, render_v3
    ffmpeg = os.environ['CLIPPIE_FFMPEG']
    source = output / 'fixture.mp4'
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=15',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True, timeout=30)
    words = [{'id': 0, 'text': 'Clippie', 'start': 0.05, 'end': 0.4},
             {'id': 1, 'text': 'works', 'start': 0.45, 'end': 0.9}]
    sources = {'fixture': {'duration': 1.0, 'source_path': str(source), 'words': words}}
    timeline = {'schema_version': 3, 'canvas': {'width': 360, 'height': 640, 'fps': 30},
                'segments': [{'id': 's1', 'source_id': 'fixture', 'start': 0.0, 'end': 1.0}],
                'captions': {'template_id': 'bold_pop'}, 'audio': {'normalize': False}, 'overlays': []}
    result = render_v3.render(timeline, sources, output / 'render')
    if result['width'] != 360 or result['height'] != 640 or 'Clippie' not in result['srt_path'].read_text():
        raise RuntimeError('Captioned export check failed.')
    report['render'] = {key: str(value) if isinstance(value, Path) else value for key, value in result.items()}
    if args.whisper_model:
        if not args.speech_fixture:
            parser.error('--whisper-model requires --speech-fixture')
        os.environ['CLIPPIE_WHISPER_MODEL'] = str(Path(args.whisper_model).resolve(strict=True))
        transcript = media.transcribe(Path(args.speech_fixture), output / 'transcript', provider='mlx', language='en')
        if not transcript['words']:
            raise RuntimeError('Local Whisper returned no words from the speech fixture.')
        report['whisper'] = {'word_count': len(transcript['words']),
                             'text': ' '.join(word['text'] for word in transcript['words']),
                             'model_origin': 'explicit pre-existing local weights; provisioning not tested'}
    if args.embedding_model:
        from clippie import analysis
        os.environ['CLIPPIE_EMBEDDING_MODEL'] = str(Path(args.embedding_model).resolve(strict=True))
        vector = analysis.encode(['A local packaging test.'])
        if vector.shape != (1, 768):
            raise RuntimeError('Unexpected local embedding shape.')
        report['embeddings'] = {'shape': list(vector.shape), 'model_origin': 'explicit pre-existing local weights'}
    report['seconds'] = round(time.monotonic() - started, 3)
    (output / 'runtime-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'event': 'runtime_probe', 'passed': True, 'report': str(output / 'runtime-report.json')}))


if __name__ == '__main__':
    main()
