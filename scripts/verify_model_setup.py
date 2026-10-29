#!/usr/bin/env python3
"""Exercise fresh model preparation with pinned local fixtures and a tiny real download.

Large publisher artifacts must already exist and are served through an in-memory
HTTP transport, not downloaded again. YuNet uses its real pinned HTTPS source.
This tests conversion/installation/inference, not fresh large-file network access
or acceptance of publisher terms by a new end user.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time

import httpx

from clippie.model_downloads import download_verified
from clippie.model_setup import ModelSetup


class FileStream(httpx.SyncByteStream):
    def __init__(self, path, offset=0):
        self.path, self.offset = path, offset

    def __iter__(self):
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            while chunk := stream.read(1024 * 1024):
                yield chunk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--whisper-checkpoint', required=True, type=Path)
    parser.add_argument('--embedding-source', required=True, type=Path)
    parser.add_argument('--speech-fixture', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'large_download_transport': 'local pinned artifacts over MockTransport',
              'fresh_large_network_download': False, 'new_user_terms_acceptance': False, 'models': {}}
    with tempfile.TemporaryDirectory(prefix='clippie-fresh-models-') as temporary:
        root = Path(temporary)
        for name in ('CLIPPIE_WHISPER_MODEL', 'CLIPPIE_EMBEDDING_MODEL', 'CLIPPIE_YUNET_MODEL'):
            os.environ.pop(name, None)
        os.environ.update(CLIPPIE_DATA_DIR=str(root), CLIPPIE_DESKTOP='1', HF_HUB_OFFLINE='1',
                          TRANSFORMERS_OFFLINE='1', HF_HOME=str(root / 'cache/huggingface'),
                          XDG_CACHE_HOME=str(root / 'cache'), NUMBA_CACHE_DIR=str(root / 'cache/numba'))
        installer = ModelSetup(root)
        files = {}
        for model in installer.models.values():
            for artifact in model['artifacts']:
                if model['id'] == 'whisper':
                    files[artifact['url']] = args.whisper_checkpoint.resolve(strict=True)
                elif model['id'] == 'embeddings':
                    files[artifact['url']] = (args.embedding_source / artifact['name']).resolve(strict=True)

        def serve(request):
            path = files[str(request.url)]
            size = path.stat().st_size
            offset = int(request.headers.get('range', 'bytes=0-').split('=')[1].split('-')[0])
            headers = {'content-length': str(size - offset)}
            if offset:
                headers['content-range'] = f'bytes {offset}-{size - 1}/{size}'
            return httpx.Response(206 if offset else 200, headers=headers, stream=FileStream(path, offset))

        client = httpx.Client(transport=httpx.MockTransport(serve), trust_env=False)
        def download(artifact, destination, *, cancel, progress):
            return download_verified(artifact, destination, cancel=cancel, progress=progress,
                                     client=client if artifact['url'] in files else None)
        installer.downloader = download
        for ident in installer.models:
            started = time.monotonic()
            installer.start(ident, accept_terms=True)
            installer.thread.join(timeout=300)
            if installer.thread.is_alive():
                installer.shutdown()
                raise RuntimeError('Model preparation timed out.')
            status = next(model for model in installer.snapshot()['models'] if model['id'] == ident)
            if not status['ready'] or not status['verified']:
                raise RuntimeError(f'{ident} preparation failed: {status["error"]}')
            report['models'][ident] = {'verified': True, 'seconds': round(time.monotonic() - started, 3)}
        client.close()
        from clippie import analysis, media
        speech = media.transcribe(args.speech_fixture.resolve(), root / 'speech', provider='mlx', language='en')
        if not speech['words']:
            raise RuntimeError('Fresh converted Whisper produced no speech.')
        report['whisper_word_count'] = len(speech['words'])
        report['embedding_shape'] = list(analysis.encode(['Fresh model installation check.']).shape)
        if report['embedding_shape'] != [1, 768]:
            raise RuntimeError('Fresh embedding model produced wrong shape.')
        import cv2
        import numpy as np
        detector = cv2.FaceDetectorYN_create(str(installer.target(installer.models['vision'])), '', (320, 320))
        _, faces = detector.detect(np.zeros((320, 320, 3), dtype=np.uint8))
        report['vision_inference'] = {'passed': True, 'blank_frame_detections': 0 if faces is None else len(faces)}
        report['receipt_count'] = len(list(installer.work.joinpath('receipts').glob('*.json')))
    report['passed'] = True
    (output / 'fresh-model-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
