"""Immutable, lease-owned render artifacts for reviewed collection revisions."""
import copy
import hashlib
from pathlib import Path
import re

from . import render


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value):
        raise ValueError('Invalid artifact identity')
    return value


def render_revision(ctx, revision, sources, root, kind='preview', *, export_decisions=()):
    ctx.checkpoint()
    if kind not in {'preview', 'export'}:
        raise ValueError('Invalid render kind')
    if ctx.task.get('short_id') != revision.get('short_id'):
        raise ValueError('Revision does not belong to this short')
    document = revision['document']
    if kind == 'export':
        from .collection_store import require_export_eligible
        require_export_eligible(revision, export_decisions)
    held_preview = (kind == 'preview' and document.get('inspection_only') is True
                    and document.get('editorial', {}).get('status') in {'needs_revision', 'uncertain'})
    if document.get('technical') != 'pass' or (not held_preview and document.get('editorial', {}).get('status') != 'pass'):
        raise ValueError('Editorial review must pass before rendering')
    timeline = copy.deepcopy(document['timeline'])
    aspect = timeline.get('aspect_ratio', '9:16')
    if aspect not in {'9:16', '16:9'}:
        raise ValueError('Invalid render aspect')
    size = (360, 640) if kind == 'preview' else (1080, 1920)
    if aspect == '16:9':
        size = size[::-1]
    if kind == 'export':
        size = (timeline.get('width', size[0]), timeline.get('height', size[1]))
    timeline.update(width=size[0], height=size[1])
    revision_id = _identifier(revision['id'])
    attempt = f"{kind}-{_identifier(ctx.task['id'])}-{_identifier(ctx.task['lease_token'])}"
    directory = Path(root).resolve() / 'collection-artifacts' / revision_id / attempt
    directory.mkdir(parents=True, exist_ok=False)
    result = render.render_production(timeline, sources, directory, process_owner=ctx)
    ctx.checkpoint()
    with render.process_ownership(ctx):
        probe = render._probe(result['video_path'])
    expected = sum(float(clip['end']) - float(clip['start']) for clip in timeline['clips'])
    if (not probe['has_audio'] or not probe.get('video_codec')
            or (probe['width'], probe['height']) != size
            or abs(probe['duration'] - expected) > max(.15, expected * .01)):
        raise ValueError('Rendered media failed validation')
    digest = hashlib.sha256()
    with Path(result['video_path']).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            ctx.checkpoint()
            digest.update(chunk)
    ctx.checkpoint()
    return {'revision_id': revision_id, 'short_id': revision['short_id'], 'kind': kind,
            'profile': {'width': size[0], 'height': size[1], 'aspect_ratio': aspect,
                        'fps': timeline.get('fps', 30)},
            'video_path': str(result['video_path']), 'srt_path': str(result['srt_path']),
            'manifest_path': str(result['manifest_path']), 'sha256': digest.hexdigest(),
            'probe': probe, 'metrics': result['metrics'], 'warnings': result['warnings']}
