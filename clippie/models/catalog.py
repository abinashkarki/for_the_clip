"""Live model lists per provider and routing presets.

Free tiers change often, so presets are starting points that the Settings page checks against the provider's
own model list before applying. Listing calls send only the key; no prompt content.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .providers.chat_compat import ENDPOINTS, url_for

from ..paths import resource_root

HERE = resource_root() / 'models'
GEMINI_MODELS = 'https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000'
STATIC = {'anthropic': 'https://api.anthropic.com/v1/models?limit=1000', 'openai': 'https://api.openai.com/v1/models'}


class ListError(RuntimeError):
    pass


def presets():
    return json.loads((HERE / 'presets.json').read_text())['presets']


def _get(url, headers, transport=None):
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, transport=transport) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError:
        raise ListError('Could not reach the provider to list models.') from None
    if response.status_code in (401, 403):
        raise ListError('The provider rejected this key.')
    if response.status_code != 200:
        raise ListError(f'The provider answered {response.status_code} when listing models.')
    try:
        return response.json()
    except ValueError:
        raise ListError('The provider returned an unreadable model list.') from None


def available(provider, key, *, transport=None):
    """Model ids the provider offers this key, newest naming first where the API orders them."""
    if provider == 'vertex':
        raise ListError('Google Cloud uses ADC. Model access depends on the project and location; verify with a model call.')
    if provider == 'gemini':
        data = _get(GEMINI_MODELS, {'x-goog-api-key': key}, transport)
        return [item['name'].removeprefix('models/') for item in data.get('models', [])
                if 'generateContent' in item.get('supportedGenerationMethods', [])]
    if provider == 'anthropic':
        data = _get(STATIC['anthropic'], {'x-api-key': key, 'anthropic-version': '2023-06-01'}, transport)
        return [item['id'] for item in data.get('data', [])]
    if provider in ('openai', *ENDPOINTS):
        url = STATIC['openai'] if provider == 'openai' else url_for(provider).removesuffix('/chat/completions') + '/models'
        headers = {'Authorization': f'Bearer {key}'} if key else {}
        data = _get(url, headers, transport)
        ids = [item['id'] for item in data.get('data', []) if isinstance(item, dict) and item.get('id')]
        if provider == 'openrouter':
            free = [i for i in ids if i.endswith(':free')]
            return free + [i for i in ids if not i.endswith(':free')]
        return ids
    raise ListError('This provider has no model list.')


def resolve_preset(preset_id, offered):
    """Routing for a preset, picking each stage's first candidate the provider actually offers.

    `offered` maps provider -> list of ids, or None when the list could not be fetched (then the first
    candidate is used). Returns (routing, notes).
    """
    preset = next((p for p in presets() if p['id'] == preset_id), None)
    if preset is None:
        raise KeyError(preset_id)
    defaults, notes = {}, []
    for stage, choice in preset['stages'].items():
        provider, candidates = choice['provider'], choice['models']
        ids = offered.get(provider)
        pick = next((m for m in candidates if ids is None or m in ids), None)
        if pick is None:
            pick = candidates[0]
            notes.append(f'{stage}: none of {", ".join(candidates)} is listed for this key; kept {pick}.')
        defaults[stage] = {'provider': provider, 'model': pick}
    return {'defaults': defaults, 'fallbacks': {}}, notes
