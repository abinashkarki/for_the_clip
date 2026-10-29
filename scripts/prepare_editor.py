"""Download the pinned, optional local text editor. No cloud inference or key."""
import hashlib
import json
from pathlib import Path
import sys
import shutil
from concurrent.futures import ThreadPoolExecutor
import httpx

from clippie.local_editor import model_directory

MODEL = 'mlx-community/Qwen3-4B-Instruct-2507-4bit'
REVISION = '50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b'
MODEL_SHA = '2a73c6c248601ab904e035548abd8e6abb65ea27dcb5f342fb0a8910eb44173f'
FILES = ['config.json', 'generation_config.json', 'tokenizer.json', 'tokenizer_config.json',
         'special_tokens_map.json', 'added_tokens.json', 'vocab.json', 'merges.txt',
         'chat_template.jinja', 'model.safetensors.index.json', 'model.safetensors']
MODEL_BYTES = 2263022417


def checkpoint(root):
    """Resume a cached prefix and fetch four disjoint ranges, checksum before use."""
    target = root/'model.safetensors'
    prefix = root/'model.safetensors.part'
    offset = prefix.stat().st_size if prefix.exists() else 0
    if offset > MODEL_BYTES:
        raise RuntimeError('Cached checkpoint prefix is larger than the expected model.')
    span = (MODEL_BYTES-offset+3)//4
    parts = []
    def fetch(bounds):
        start, end, path = bounds
        cached = path.stat().st_size if path.exists() else 0
        if cached > end-start+1:
            raise RuntimeError('Cached model range has an invalid length.')
        if cached == end-start+1:
            return
        begin = start+cached
        with httpx.Client(follow_redirects=True, timeout=90) as client:
            with client.stream('GET', f'https://huggingface.co/{MODEL}/resolve/{REVISION}/model.safetensors',
                               headers={'Range': f'bytes={begin}-{end}'}) as response:
                response.raise_for_status()
                if response.status_code != 206 or response.headers.get('content-range') != f'bytes {begin}-{end}/{MODEL_BYTES}':
                    raise RuntimeError('Model server did not honor the requested byte range.')
                with path.open('ab') as f:
                    for chunk in response.iter_bytes(1024*1024):
                        f.write(chunk)
        if path.stat().st_size != end-start+1:
            raise RuntimeError('Downloaded model range has an invalid length.')
        print('Completed model range', start, end, flush=True)
    if offset < MODEL_BYTES:
        for start in range(offset, MODEL_BYTES, span):
            end = min(MODEL_BYTES-1, start+span-1)
            parts.append((start, end, root/f'model.range-{start}-{end}.part'))
        print('Resuming checkpoint prefix', offset, 'with', len(parts), 'parallel ranges', flush=True)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(fetch, parts))
    assembled = root/'model.safetensors.assembled'
    with assembled.open('wb') as output:
        for path in ([prefix] if offset else []) + [part[2] for part in parts]:
            with path.open('rb') as source:
                shutil.copyfileobj(source, output, length=1024*1024)
    with assembled.open('rb') as f:
        if hashlib.file_digest(f, 'sha256').hexdigest() != MODEL_SHA:
            raise RuntimeError('Assembled model checksum failed; cached ranges were preserved.')
    assembled.replace(target)
    prefix.unlink(missing_ok=True)
    for _, _, path in parts:
        path.unlink()


def prepare(root=None):
    root = Path(root).expanduser().resolve() if root is not None else model_directory()
    root.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(90, connect=30)) as client:
        for name in FILES:
            target = root/name
            if target.is_file() and target.stat().st_size:
                if name == 'model.safetensors':
                    with target.open('rb') as f:
                        if hashlib.file_digest(f, 'sha256').hexdigest() != MODEL_SHA:
                            raise RuntimeError('Existing editor checkpoint checksum differs; preserve and inspect it.')
                continue
            if name == 'model.safetensors':
                checkpoint(root)
                continue
            part = target.with_suffix(target.suffix+'.part')
            offset = part.stat().st_size if part.exists() else 0
            with client.stream('GET', f'https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}',
                               headers={'Range': f'bytes={offset}-'} if offset else {}) as response:
                response.raise_for_status()
                append = offset and response.status_code == 206
                received = offset if append else 0
                announced = received
                print('Downloading', name, received, flush=True)
                with part.open('ab' if append else 'wb') as f:
                    for chunk in response.iter_bytes(1024*1024):
                        f.write(chunk); received += len(chunk)
                        if received-announced > 128*1024*1024:
                            print(name, received, flush=True); announced = received
            if name == 'model.safetensors':
                with part.open('rb') as f:
                    if hashlib.file_digest(f, 'sha256').hexdigest() != MODEL_SHA:
                        raise RuntimeError('Downloaded editor checkpoint checksum failed.')
            part.replace(target)
    (root/'clippie-model.json').write_text(json.dumps({'model': MODEL, 'revision': REVISION,
                                                     'sha256': MODEL_SHA}, indent=2))
    print(root, flush=True)


if __name__ == '__main__':
    prepare(sys.argv[1] if len(sys.argv)>1 else None)
