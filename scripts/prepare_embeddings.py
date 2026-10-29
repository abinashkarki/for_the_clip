"""Fetch a pinned public EmbeddingGemma model over ordinary HTTPS, resumably."""
import hashlib
import json
from pathlib import Path
import sys
import httpx

from clippie.analysis import model_directory

REVISION = '914f7f89142e33e77833254d9c9b90c3cef7303b'
FILES = ['config.json','config_sentence_transformers.json','modules.json','1_Pooling/config.json',
         '2_Normalize/config.json','sentence_bert_config.json','tokenizer.json','tokenizer.model',
         'tokenizer_config.json','chat_template.jinja','preprocessor_config.json','processor_config.json','model.safetensors']
MODEL_SHA = '197a32965d4b1105faf060417baa899e193fb73cd401f42ec9295234d5553d79'


def prepare(root=None):
    root = Path(root).expanduser().resolve() if root is not None else model_directory()
    root.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(90,connect=30)) as client:
        for name in FILES:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size:
                if name != 'model.safetensors':
                    continue
                with target.open('rb') as f:
                    if hashlib.file_digest(f,'sha256').hexdigest() == MODEL_SHA:
                        continue
                raise RuntimeError('Existing model checksum mismatch; preserve the file and inspect it.')
            part = target.with_suffix(target.suffix + '.part')
            offset = part.stat().st_size if part.exists() else 0
            url = f'https://huggingface.co/google/embeddinggemma-2/resolve/{REVISION}/{name}'
            headers = {'Range':f'bytes={offset}-'} if offset else {}
            with client.stream('GET',url,headers=headers) as response:
                response.raise_for_status()
                append = offset and response.status_code == 206
                received = offset if append else 0
                last_print = received
                print(f'Downloading {name} ({received:,} bytes cached)',flush=True)
                with part.open('ab' if append else 'wb') as output:
                    for chunk in response.iter_bytes(1024*1024):
                        output.write(chunk)
                        received += len(chunk)
                        if received - last_print >= 64*1024*1024:
                            print(f'{name}: {received:,} bytes',flush=True)
                            last_print = received
            if name == 'model.safetensors':
                with part.open('rb') as f:
                    actual = hashlib.file_digest(f,'sha256').hexdigest()
                if actual != MODEL_SHA:
                    raise RuntimeError(f'Model checksum failed: {actual}')
            part.replace(target)
        (root / 'clippie-model.json').write_text(json.dumps({'model':'google/embeddinggemma-2','revision':REVISION,'sha256':MODEL_SHA},indent=2))
    print(root,flush=True)
    return root


if __name__ == '__main__':
    prepare(sys.argv[1] if len(sys.argv)>1 else None)
