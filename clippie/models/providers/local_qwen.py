"""Local Qwen editor worker. No streaming: only the total timeout applies."""
from __future__ import annotations

from ..core import ModelError
from ._http import Raw


def call(req, model, key):
    from ... import local_editor
    if req.images:
        raise ModelError('config', 'The local editor does not accept pictures.')
    messages = ([{'role': 'system', 'content': req.system}] if req.system else [])
    messages.append({'role': 'user', 'content': req.prompt})
    try:
        envelope = local_editor.generate(messages, max_tokens=min(req.max_output_tokens, 1200))
    except ValueError as error:
        raise ModelError('config', str(error)) from None
    except RuntimeError as error:
        text = str(error)
        raise ModelError('timeout' if 'exceeded' in text else 'service', text) from None
    raw = Raw()
    raw.text = envelope.get('text', '')
    usage = envelope.get('usage', {})
    raw.usage = {'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                 'thinking_tokens': None, 'cached_tokens': None, 'total_tokens': usage.get('total_tokens')}
    raw.model_version = local_editor.MODEL_REVISION
    raw.finish_reason = {'stop': 'stop', 'length': 'length'}.get(envelope.get('finish_reason'), envelope.get('finish_reason'))
    return raw
