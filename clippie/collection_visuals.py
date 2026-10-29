"""Bounded supporting-picture retrieval and immutable sampled-image references."""
import base64
import io
import json
import math
from pathlib import Path

from PIL import Image

from . import render, source_visuals
from .source_evidence import _hash


def source_snapshot(source):
    duration = source.get('duration')
    if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('Picture source needs a finite positive duration.')
    path = render._source_path(source, source['id']).resolve()
    stat = path.stat()
    identity = {'path': str(path), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'inode': stat.st_ino, 'device': stat.st_dev}
    return {'source_id': source['id'], 'duration': duration, 'media_identity': identity}


def picture_payload(sample):
    with Image.open(sample['path']) as image:
        image = image.convert('RGB'); image.thumbnail((240, 135))
        buffer = io.BytesIO(); image.save(buffer, 'JPEG', quality=45)
        if buffer.tell() > 12_000:
            image.thumbnail((160, 90))
            buffer = io.BytesIO(); image.save(buffer, 'JPEG', quality=35)
    if buffer.tell() > 12_000:
        raise ValueError('Supporting picture exceeds the bounded JPEG envelope.')
    return {'id': sample['id'], 'source': sample['source_id'], 'at': sample['at'],
            'data': base64.b64encode(buffer.getvalue()).decode('ascii')}


def _record(prepared, sample):
    manifest = prepared['artifact']['manifest']
    start = max(0., sample['at'] - 1.)
    end = min(manifest['duration'], start + 2.)
    return {'id': sample['id'], 'source_id': sample['source_id'], 'at': sample['at'],
            'start': start, 'end': end, 'artifact_key': prepared['cache_artifact_key'],
            'media_identity': manifest['media_identity'], 'picture': picture_payload(sample)}


def _semantic_matches(sources, source_root, query):
    indexes = []
    # Existing prepared indexes only: never build one or download weights here.
    for sid in sources:
        directory = Path(source_root) / sid / 'analysis'
        path = directory / 'analysis.json'
        if path.is_file():
            try:
                info = json.loads(path.read_text())
            except (OSError, ValueError):
                raise ValueError('Saved picture index is damaged; inspect or rebuild it in Library.') from None
            if info.get('semantic'):
                indexes.append((directory, info))
    if not indexes:
        return [], 'No prepared semantic picture index; using labelled coverage samples.'
    try:
        from . import analysis
    except ImportError:
        return [], 'Local semantic runtime unavailable; using labelled coverage samples.'
    valid = [directory for directory, info in indexes
             if info.get('source_id') in sources and
             info.get('fingerprint') == analysis.source_fingerprint(sources[info['source_id']]) and
             info.get('model_revision') == analysis.MODEL_REVISION]
    if not valid:
        return [], 'Prepared picture indexes are outdated; using labelled coverage samples.'
    status = analysis.semantic_status()
    if not status['installed'] or not status['weights_ready']:
        return [], 'Local semantic weights unavailable; using labelled coverage samples.'
    return analysis.search(valid, query[:1600], limit=12, use_embeddings=True, modality='image'), None


def for_candidate(root, source_root, sources, constraints, candidate, process_owner=None):
    """Retrieve at most two stills. Similarity suggests relevance; it proves no event."""
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 12:
        raise ValueError('Supporting-picture scope must contain one through twelve sources.')
    roles = constraints.get('source_roles', {})
    picture_only = {sid: source for sid, source in sources.items() if roles.get(sid, 'primary') == 'picture_only'}
    eligible = picture_only or sources
    query = ' '.join(str(candidate.get(field, '')) for field in ('title', 'angle'))
    if process_owner: process_owner.checkpoint()
    matches, reason = _semantic_matches(eligible, source_root, query)
    selected, seen = [], set()
    for match in matches:
        sid, at = match.get('source_id'), match.get('at')
        if (sid in eligible and not isinstance(at, bool) and isinstance(at, (int, float))
                and math.isfinite(at) and 0 <= at < eligible[sid]['duration'] and (sid, at) not in seen):
            selected.append((sid, float(at))); seen.add((sid, at))
        if len(selected) == 2: break
    mode = 'semantic_index' if selected else 'coverage_samples'
    if not selected:
        ids = sorted(eligible)
        offset = int(_hash(candidate.get('angle', ''))[:8], 16) % len(ids)
        for index, fraction in enumerate((.25, .75)):
            sid = ids[(offset + index) % len(ids)]
            selected.append((sid, eligible[sid]['duration'] * fraction))
        reason = reason or 'No usable semantic image matches; using labelled coverage samples.'
    rows = []
    for sid, at in selected:
        if process_owner: process_owner.checkpoint()
        prepared = source_visuals.prepare_samples(eligible[sid], Path(root) / 'source-visuals', [at], process_owner=process_owner)
        rows.append(_record(prepared, prepared['artifact']['samples'][0]))
    return rows, {'mode': mode, 'reason': reason, 'sample_count': len(rows),
                  'limit': 'At most two sparse stills; relevance and unseen motion need output review.'}


def for_plan(root, plan):
    rows = []
    evidence_rows = plan.get('visual_evidence', [])
    if not isinstance(evidence_rows, list) or len(evidence_rows) > 2:
        raise ValueError('Supporting-picture proof must contain at most two samples.')
    for evidence in evidence_rows:
        prepared = source_visuals.load_samples(Path(root) / 'source-visuals', evidence['artifact_key'])
        sample = next((s for s in prepared['artifact']['samples'] if s['id'] == evidence['id']), None)
        if sample is None:
            raise ValueError('Supporting sample is unavailable.')
        row = _record(prepared, sample)
        if any(row[field] != evidence.get(field) for field in ('source_id', 'at', 'start', 'end', 'media_identity')):
            raise ValueError('Supporting picture differs from its saved visual evidence.')
        rows.append(row)
    return rows
