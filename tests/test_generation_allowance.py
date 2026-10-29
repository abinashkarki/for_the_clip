"""Default allowance covers the actual cold graph without contacting a provider."""
from types import SimpleNamespace

import pytest

from clippie import collection_generation as generation
from clippie.collection_store import CollectionStore


def long_source(monkeypatch):
    # Four transcript windows; each complete unit is transported in four map
    # packets. Dense packet bodies exceed the standard reservation envelope.
    units = [{'id': f'u{i}', 'start': i * 100, 'text': 'speech ' * 8500} for i in range(4)]
    packets = [{'id': f'p{i}', 'fragments': [{'unit_id': f'u{i // 4}', 'text': 'speech ' * 3000}]}
               for i in range(16)]
    prepared = {'artifact': {'manifest': {'source_id': 's'}, 'units': units, 'packets': packets}}
    monkeypatch.setattr(generation.source_evidence, 'evidence_key', lambda source: 'evidence-v1')
    monkeypatch.setattr(generation.source_evidence, 'prepare_evidence', lambda *args: prepared)
    return SimpleNamespace(get=lambda identity: {'id': identity}), prepared


def start(storage, sources, **kwargs):
    cid = storage.create_collection(['s'], budget=kwargs.pop('budget', None))['id']
    run = generation.start_generation(storage, sources, cid, None, dispatch_work=False, **kwargs)
    return cid, run


def test_default_allowance_admits_cold_sixteen_map_four_window_graph(tmp_path, monkeypatch):
    sources, prepared = long_source(monkeypatch)
    storage = CollectionStore(tmp_path)
    cid, run = start(storage, sources)
    tasks = storage.list_tasks(cid)
    maps = [task for task in tasks if task['stage'] == 'map_source']
    assert len(maps) == 16
    assert all(task['reservation']['tokens'] > generation.provider.RESERVATION_TOKENS for task in maps)
    windows = generation.editor._windows(generation.editor._transcript(
        generation.editor._artifacts({'s': prepared}), {}), generation.editor.DISCOVERY_WINDOW_CHARS)
    assert len(windows) == 4
    # Exercise actual atomic claims/admission rather than only checking a formula.
    for task in maps:
        claimed = storage.claim_task(collection_id=cid)
        assert claimed is not None and claimed['stage'] == 'map_source'
        storage.finish_task(claimed['id'], claimed['lease_token'], claimed['input_version'], result={})
    discovery = storage.claim_task(collection_id=cid)
    assert discovery is not None and discovery['stage'] == 'discover'
    volume = generation.provider.VOLUME_RESERVATION_TOKENS
    for number in range(len(windows)):
        identity = f'window-{number}'
        storage.admit_invocation(discovery['id'], discovery['lease_token'], identity, volume)
        storage.reconcile_invocation(discovery['id'], discovery['lease_token'], identity, 100)
    storage.finish_task(discovery['id'], discovery['lease_token'], discovery['input_version'], result={})
    for stage in ('review_batch', 'repair_batch', 'review_batch'):
        # These are separate durable tasks with one volume call each.
        ordinal = len(storage.list_tasks(cid))
        storage.enqueue_task(cid, stage, 'v', f'{stage}-{ordinal}', reserved_calls=1, reserved_tokens=volume)
        claimed = storage.claim_task(collection_id=cid)
        assert claimed is not None and claimed['stage'] == stage
        storage.admit_invocation(claimed['id'], claimed['lease_token'], f'physical-{ordinal}', volume)
        storage.reconcile_invocation(claimed['id'], claimed['lease_token'], f'physical-{ordinal}', 100)
        storage.finish_task(claimed['id'], claimed['lease_token'], 'v', result={})
    collection = storage.get_collection(cid)
    assert collection['usage']['calls'] == 23
    assert all(collection['usage'][key] <= collection['budget'][key] for key in ('tokens', 'calls'))
    assert not any(t['status'] == 'held_budget' for t in storage.list_tasks(cid))


@pytest.mark.parametrize('options', [{'calls': 3}, {'budget': {'calls': 2, 'tokens': 100}}])
def test_explicit_allowance_is_not_expanded_to_cover_long_source(tmp_path, monkeypatch, options):
    sources, _ = long_source(monkeypatch)
    storage = CollectionStore(tmp_path)
    cid, run = start(storage, sources, **options)
    collection = storage.get_collection(cid)
    if 'budget' in options:
        assert collection['budget'] == options['budget']
    else:
        assert collection['budget']['calls'] == run['configuration']['calls'] == 3
    if 'budget' in options:
        assert storage.claim_task(collection_id=cid) is None
    else:
        for _ in range(3):
            claimed = storage.claim_task(collection_id=cid)
            assert claimed is not None
            storage.finish_task(claimed['id'], claimed['lease_token'], claimed['input_version'], result={})
        assert storage.claim_task(collection_id=cid) is None
    assert any(task['status'] == 'held_budget' for task in storage.list_tasks(cid))
