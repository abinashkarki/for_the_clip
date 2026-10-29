"""Model gateway API: call log, summary and routing."""
from fastapi import APIRouter, HTTPException, Query

from .models import gateway
from .models.core import STAGES

router = APIRouter(prefix='/api/models')


@router.get('/calls')
def list_calls(limit: int = Query(50, ge=1, le=500), stage: str | None = None, provider: str | None = None,
               model: str | None = None, status: str | None = None, collection_id: str | None = None):
    return {'calls': gateway.calls(limit, stage=stage, provider=provider, model=model, status=status,
                                   collection_id=collection_id)}


@router.get('/calls/{call_id}')
def get_call(call_id: str):
    row = gateway.get_call(call_id)
    if row is None:
        raise HTTPException(404, 'Model call not found.')
    return row


@router.get('/summary')
def summary():
    return {'prices_as_of': gateway.prices()['as_of'], 'groups': gateway.summary()}


@router.get('/routing')
def get_routing():
    return gateway.routing()


@router.put('/routing')
def put_routing(body: dict):
    try:
        return gateway.save_routing(body)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get('/catalog')
def catalog():
    """What the routing editor can offer: stages, providers and the models with a known price."""
    known = gateway.prices()['models']
    return {'stages': list(STAGES), 'providers': [{'id': name, 'label': gateway.NAMES[name]} for name in gateway.PROVIDERS],
            'models': {name: sorted(known.get(name, {})) for name in gateway.PROVIDERS}}


def _keys():
    from . import app
    return app.KEYS


@router.get('/available/{provider}')
def available_models(provider: str):
    from .models import catalog
    if provider not in gateway.PROVIDERS or provider == 'local_qwen':
        raise HTTPException(404, 'Unknown provider.')
    key = _keys().get(provider) or ''
    if provider not in gateway.KEYLESS and not key:
        raise HTTPException(422, f'Add a {gateway.NAMES[provider]} API key first.')
    try:
        return {'provider': provider, 'models': catalog.available(provider, key)}
    except catalog.ListError as error:
        raise HTTPException(502, str(error)) from None


@router.get('/presets')
def list_presets():
    from .models import catalog
    return {'presets': [{key: p[key] for key in ('id', 'name', 'description')}
                        | {'providers': sorted({s['provider'] for s in p['stages'].values()})} for p in catalog.presets()]}


@router.post('/routing/preset/{preset_id}')
def apply_preset(preset_id: str):
    """Resolve a preset against each provider's live model list and save it as the routing."""
    from .models import catalog
    keys = _keys()
    try:
        preset = next(p for p in catalog.presets() if p['id'] == preset_id)
    except StopIteration:
        raise HTTPException(404, 'Unknown preset.') from None
    offered = {}
    for provider in {s['provider'] for s in preset['stages'].values()}:
        key = keys.get(provider) or ''
        if provider not in gateway.KEYLESS and not key:
            raise HTTPException(422, f'Add a {gateway.NAMES[provider]} API key before using this preset.')
        try:
            offered[provider] = catalog.available(provider, key)
        except catalog.ListError:
            offered[provider] = None
    routing, notes = catalog.resolve_preset(preset_id, offered)
    try:
        saved = gateway.save_routing(routing)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {'routing': saved, 'notes': notes}


@router.post('/calls/{call_id}/replay')
def replay_call(call_id: str, body: dict):
    """Re-send an archived text-only call to the same or another model, for comparison and debugging."""
    from .models import ModelError
    provider, model = body.get('provider'), body.get('model')
    row = gateway.get_call(call_id)
    if row is None:
        raise HTTPException(404, 'Model call not found.')
    provider = provider or row['provider']
    model = model or row['model']
    if provider not in gateway.PROVIDERS or not isinstance(model, str) or not model:
        raise HTTPException(422, 'Choose a known provider and a model.')
    try:
        result = gateway.replay(call_id, provider, model, keys=_keys(), options=body.get('options'))
    except ModelError as error:
        return {'ok': False, 'category': error.category, 'message': str(error),
                'provider_detail': getattr(error, 'detail', None), 'partial': getattr(error, 'partial', None),
                'call_id': error.call_id}
    return {'ok': True, 'call_id': result.call_id, 'model': result.model, 'latency_ms': result.latency_ms,
            'usage': result.usage, 'cost_usd': result.cost_usd, 'json': result.json}
