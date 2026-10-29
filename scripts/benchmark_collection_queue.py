"""Bounded provider-free independent-source sequential measurements, not a durable queue test."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from benchmark_collection_local import ROOT, bounded_seconds


def summarize(children):
    profiles = {}
    sources = []
    for child in children:
        raw = child.get('raw_report') or {}
        groups = {}
        for kind, samples in (('evidence', raw.get('evidence', [])), ('render', raw.get('renders', []))):
            for sample in samples:
                label = ('evidence/' + sample['warmth'] if kind == 'evidence' else
                         'render/' + 'x'.join(map(str, sample['profile'])))
                for target in (groups, profiles):
                    entry = target.setdefault(label, {'samples': 0, 'wall_seconds': 0.,
                        'parent_cpu_seconds': 0., 'reaped_child_cpu_seconds': 0., 'video_bytes': 0})
                    entry['samples'] += 1
                    for metric in ('wall_seconds', 'parent_cpu_seconds', 'reaped_child_cpu_seconds', 'video_bytes'):
                        entry[metric] += sample.get(metric, 0)
        sources.append({'source_id': child['source_id'], 'status': child['status'],
                        'process_wall_seconds': child.get('process_wall_seconds'), 'profiles': groups})
    complete = [c for c in children if c['status'] == 'complete']
    return {'by_profile': profiles, 'per_source': sources,
            'one_source_sequential': sources[0] if sources else None,
            'four_source_sequential': {'process_wall_seconds': sum(c['process_wall_seconds'] for c in children),
                'profiles': profiles} if len(children) == len(complete) == 4 else None,
            'denominators': {'attempted_sources': len(children), 'completed_independent_sources': len(complete),
                'distinct_passages_measured': sum(bool((c.get('raw_report') or {}).get('renders')) for c in children),
                'render_samples': sum(len((c.get('raw_report') or {}).get('renders', [])) for c in children),
                'verified_render_samples': sum(s.get('verified') is True for c in children for s in (c.get('raw_report') or {}).get('renders', [])),
                'evidence_samples': sum(len((c.get('raw_report') or {}).get('evidence', [])) for c in children),
                'verified_evidence_samples': sum(c.get('evidence_verified_samples', 0) for c in children),
                'human_reviews': 0}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-id', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=bounded_seconds, default=12.)
    parser.add_argument('--repeats', type=int, choices=range(1, 4), default=2)
    args = parser.parse_args()
    if not 1 <= len(args.source_id) <= 4 or len(set(args.source_id)) != len(args.source_id):
        parser.error('Provide 1 through 4 unique --source-id values')
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        parser.error('--output must be a new directory')
    output = output.resolve()
    if not output.parent.is_dir() or output == (ROOT / 'data').resolve() or (ROOT / 'data').resolve() in output.parents:
        parser.error('--output parent must exist and output must be outside data')
    from clippie import source_evidence, planner
    preflight = []
    identities = set()
    try:
        with sqlite3.connect((ROOT / 'data/clippie.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
            for source_id in args.source_id:
                row = db.execute('SELECT document FROM jobs WHERE id=?', (source_id,)).fetchone()
                if row is None or len(row[0].encode()) > 20_000_000:
                    raise ValueError(f'{source_id}: missing or oversized source document')
                source = json.loads(row[0])
                if source.get('synthetic') or source.get('source') == 'synthetic-demo':
                    raise ValueError(f'{source_id}: synthetic source rejected')
                if source.get('status') != 'ready' or not source.get('words'):
                    raise ValueError(f'{source_id}: ready source with word timings required')
                if len(source['words']) > 200_000 or len(source.get('segments', [])) > 50_000:
                    raise ValueError(f'{source_id}: transcript exceeds bounds')
                media = Path(source['source_path']).expanduser().resolve(strict=True)
                if not media.is_file():
                    raise ValueError(f'{source_id}: regular media file required')
                with media.open('rb') as handle:
                    handle.read(1)
                stat = media.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in identities:
                    raise ValueError('Independent sources must use distinct media files')
                identities.add(identity)
                source['source_path'] = str(media)
                fingerprint = source_evidence.evidence_key(source)
                start = float(source['words'][0]['start'])
                if start + args.seconds > float(source['duration']):
                    raise ValueError(f'{source_id}: requested passage exceeds duration')
                for width, height in ((360, 640), (540, 960)):
                    planner.validate_timeline({'schema_version': 2, 'aspect_ratio': '9:16', 'width': width,
                        'height': height, 'fps': 30, 'clips': [{'id': 'local-benchmark-passage',
                        'source_id': source_id, 'start': start, 'end': start + args.seconds,
                        'framing': {'mode': 'fit', 'zoom': 1, 'x': .5, 'y': .5}}], 'broll': [],
                        'titles': [], 'captions': True, 'caption_style': 'clean_focus',
                        'audio': {'normalize': True}}, {source_id: source})
                preflight.append({'source_id': source_id, 'document_sha256': hashlib.sha256(row[0].encode()).hexdigest(),
                    'evidence_fingerprint': fingerprint, 'media_path': str(media),
                    'media_stat_identity': {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                                            'inode': stat.st_ino, 'device': stat.st_dev}})
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    report = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
        'python_executable': sys.executable, 'preflight': preflight, 'children': [],
        'constraints': {'seconds': args.seconds, 'repeats': args.repeats, 'sequential_lanes': 1,
                        'provider_calls': 0, 'asr_calls': 0, 'download_calls': 0},
        'limits': ['Independent media-file identities do not prove representative content or editorial quality.',
            'This invokes local preprocessing and renders, not the actual durable batch queue.',
            'One-source total is the first selected source; four-source total exists only after four complete children.',
            'No concurrency, ETA, cloud capacity, cost or optimal preset extrapolation.',
            'Full process-tree peak RAM is unknown; OS/media caches are uncontrolled.',
            'Source media identity is stat-based, not a media content hash.'], 'peak_memory_bytes': None}
    output.mkdir(exist_ok=False)
    report_path = output / 'report.json'
    def save():
        report['aggregate'] = summarize(report['children'])
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    save()
    try:
        for index, source_id in enumerate(args.source_id, 1):
            directory = output / f'source-{index}'
            command = [sys.executable, str(ROOT / 'scripts/benchmark_collection_local.py'), '--source-id', source_id,
                       '--output', str(directory), '--seconds', str(args.seconds), '--repeats', str(args.repeats)]
            child = {'source_id': source_id, 'command': command, 'status': 'running', 'report_path': str(directory / 'report.json')}
            report['children'].append(child)
            save()
            started = time.monotonic()
            with (output / f'source-{index}.stdout.log').open('w') as stdout, (output / f'source-{index}.stderr.log').open('w') as stderr:
                result = subprocess.run(command, stdout=stdout, stderr=stderr, check=False)
            child.update(process_wall_seconds=time.monotonic()-started, returncode=result.returncode)
            raw_path = directory / 'report.json'
            if raw_path.is_file():
                child['raw_report'] = json.loads(raw_path.read_text())
            raw = child.get('raw_report') or {}
            child['status'] = 'failed'
            if result.returncode or raw.get('status') != 'complete':
                raise RuntimeError(f'{source_id}: child failed; inspect preserved raw report and stderr')
            if len(raw.get('renders', [])) != args.repeats * 2 or not all(r.get('verified') is True for r in raw['renders']):
                raise RuntimeError(f'{source_id}: render verification failed')
            evidence = raw.get('evidence', [])
            if len(evidence) != args.repeats * 2 or not all(Path(e['artifact']).is_file() and e.get('units', 0) > 0 for e in evidence):
                raise RuntimeError(f'{source_id}: evidence verification failed')
            for sample in evidence:
                artifact = json.loads(Path(sample['artifact']).read_text())
                expected_cache = 'build' if sample['warmth'] == 'cold' else 'hit'
                if (artifact.get('manifest', {}).get('fingerprint') != preflight[index-1]['evidence_fingerprint']
                        or len(artifact.get('units', [])) != sample['units']
                        or len(artifact.get('packets', [])) != sample['packets']
                        or sample['cache_status'] != expected_cache):
                    raise RuntimeError(f'{source_id}: evidence fingerprint/count/cache verification failed')
            child['evidence_verified_samples'] = len(evidence)
            if raw.get('source', {}).get('evidence_fingerprint') != preflight[index-1]['evidence_fingerprint']:
                raise RuntimeError(f'{source_id}: source evidence changed after preflight')
            child_identity = raw.get('source', {}).get('media_stat_identity', {})
            expected_identity = preflight[index-1]['media_stat_identity']
            if any(child_identity.get(key) != value for key, value in expected_identity.items()):
                raise RuntimeError(f'{source_id}: media stat identity changed after preflight')
            child['status'] = 'complete'
            save()
        report['status'] = 'complete'
    except BaseException as exc:
        report['status'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        if report['children'] and report['children'][-1]['status'] == 'running':
            report['children'][-1]['status'] = 'failed'
        raise
    finally:
        save()
        print(report_path)


if __name__ == '__main__':
    main()
