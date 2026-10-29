"""Reuse existing OpenAI Whisper weights with MLX without downloading a new model."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .paths import model_directory


def converted_model_path():
    # This is the conversion destination, not CLIPPIE_WHISPER_MODEL: that setting
    # also accepts a Hugging Face model ID and must never become a write target.
    return model_directory('whisper-small-mlx')


def convert_cached_small(source=None, target=None):
    """Convert a verified checkpoint; explicit paths support staged app setup."""
    source = Path(source) if source is not None else Path.home() / '.cache' / 'whisper' / 'small.pt'
    target = Path(target) if target is not None else converted_model_path()
    if (target / 'weights.safetensors').is_file() and (target / 'config.json').is_file():
        return target
    if not source.is_file():
        return None
    # Match the OpenAI published small-model checksum before loading a checkpoint.
    expected = '9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794'
    with source.open('rb') as cached:
        digest = hashlib.file_digest(cached, 'sha256').hexdigest()
    if digest != expected:
        raise RuntimeError('Cached Whisper Small checksum does not match the official checkpoint; the original file was left untouched.')
    import torch
    import mlx.core as mx
    from mlx_whisper.whisper import Whisper, ModelDimensions
    from mlx.utils import tree_flatten

    checkpoint = torch.load(source, map_location='cpu', weights_only=True)
    weights = checkpoint['model_state_dict']
    weights.pop('encoder.positional_embedding', None)
    converted = {}
    for key, value in weights.items():
        key = key.replace('mlp.0', 'mlp1').replace('mlp.2', 'mlp2')
        if 'conv' in key and value.ndim == 3:
            value = value.swapaxes(1, 2)
        converted[key] = mx.array(value.detach().numpy()).astype(mx.float16)
    model = Whisper(ModelDimensions(**checkpoint['dims']), mx.float16)
    model.load_weights(list(converted.items()), strict=False)
    model.set_alignment_heads(b'ABzY8DmU6=0{>%Rpa?J`kvJ6qF(V^F86#Xh7JUGMK}P<N0000')
    target.mkdir(parents=True, exist_ok=True)
    mx.save_safetensors(str(target / 'weights.safetensors'), dict(tree_flatten(model.parameters())))
    (target / 'config.json').write_text(json.dumps(asdict(model.dims), indent=2))
    (target / 'provenance.json').write_text(json.dumps({
        'source': str(source), 'sha256': digest, 'model': 'OpenAI Whisper Small',
        'conversion': 'PyTorch FP16 to MLX FP16; no quantization or model download',
        'reference': 'https://github.com/ml-explore/mlx-examples/blob/main/whisper/convert.py',
    }, indent=2))
    return target
