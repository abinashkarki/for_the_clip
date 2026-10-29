"""Bounded, provider-free local evidence/render measurement; no quality or cloud claims."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import resource
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def bounded_seconds(value):
    value = float(value)
    if not math.isfinite(value) or not 1 <= value <= 30:
        raise argparse.ArgumentTypeError('seconds must be finite and from 1 through 30')
    return value


def measure(call):
    parent = resource.getrusage(resource.RUSAGE_SELF)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    result = call()
    elapsed = time.monotonic() - started
    parent_end = resource.getrusage(resource.RUSAGE_SELF)
    child_end = resource.getrusage(resource.RUSAGE_CHILDREN)
    return result, {'wall_seconds': elapsed,
                    'parent_cpu_seconds': (parent_end.ru_utime + parent_end.ru_stime
                                           - parent.ru_utime - parent.ru_stime),
                    'reaped_child_cpu_seconds': (child_end.ru_utime + child_end.ru_stime
                                                - child.ru_utime - child.ru_stime),
                    'peak_memory_bytes': None,
                    'memory_limit': 'Unknown: cumulative getrusage maxrss is not a per-operation process-tree peak.',
                    'cpu_scope': 'This sequential operation in this harness; child CPU covers reaped subprocesses, not concurrent tree memory.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-id', required=True, help='Existing ready source in data/clippie.sqlite3 (read only)')
    parser.add_argument('--output', type=Path, required=True, help='New isolated QA directory; must not exist or be inside data')
    parser.add_argument('--seconds', type=bounded_seconds, default=12.)
    parser.add_argument('--start', type=float, default=None, help='Source start seconds; default first timed word')
    parser.add_argument('--repeats', type=int, choices=range(1, 4), default=2)
    args = parser.parse_args()
    from clippie import planner, render, source_evidence
    if args.start is not None and (not math.isfinite(args.start) or args.start < 0):
        parser.error('--start must be finite and nonnegative')
    output = args.output.expanduser().absolute()
    if output.is_symlink() or output.exists():
        parser.error('--output must be a new directory')
    output = output.resolve()
    if output == ROOT / 'data' or (ROOT / 'data').resolve() in output.parents:
        parser.error('--output must be outside data')
    if not output.parent.is_dir():
        parser.error('--output parent must already exist')
    # Do not instantiate Store: its constructor/connect configure and write SQLite.
    with sqlite3.connect((ROOT / 'data/clippie.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT document FROM jobs WHERE id=?', (args.source_id,)).fetchone()
    if row is None or len(row[0].encode()) > 20_000_000:
        parser.error('Source missing or source document exceeds 20 MB bound')
    source = json.loads(row[0])
    if source.get('status') != 'ready' or not source.get('words'):
        parser.error('Ready source with existing word timings required for real captions')
    if len(source.get('words', [])) > 200_000 or len(source.get('segments', [])) > 50_000:
        parser.error('Transcript exceeds bounded local fixture size')
    media = Path(source['source_path']).expanduser().resolve(strict=True)
    if not media.is_file():
        parser.error('Source media must be an existing regular file')
    source['source_path'] = str(media)
    fingerprint = source_evidence.evidence_key(source)
    stat = media.stat()
    start = float(source['words'][0]['start']) if args.start is None else args.start
    end = start + args.seconds
    if end > float(source['duration']):
        parser.error('--start + --seconds must fit source duration')
    if end - start < 1:
        parser.error('Source has less than one second available')
    sources = {args.source_id: source}
    base = {'schema_version': 2, 'aspect_ratio': '9:16', 'width': 360, 'height': 640,
            'fps': 30, 'clips': [{'id': 'local-benchmark-passage', 'source_id': args.source_id,
                                  'start': start, 'end': end,
                                  'framing': {'mode': 'fit', 'zoom': 1, 'x': .5, 'y': .5}}],
            'broll': [], 'titles': [], 'captions': True, 'caption_style': 'clean_focus',
            'audio': {'normalize': True}}
    base = planner.validate_timeline(base, sources)
    output.mkdir(exist_ok=False)
    report = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'status': 'running', 'environment': {'python': sys.version, 'platform': platform.platform(),
                  'machine': platform.machine(), 'renderer_sha256': hashlib.sha256(Path(render.__file__).read_bytes()).hexdigest()},
              'source': {'id': args.source_id, 'evidence_fingerprint': fingerprint,
                  'media_stat_identity': {'path': str(media), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                                          'inode': stat.st_ino, 'device': stat.st_dev},
                  'media_sha256': None, 'identity_limit': 'File stat identity, not a full media content hash.'},
              'constraints': {'one_sequential_lane': True, 'seconds_requested': args.seconds,
                  'seconds_actual': end-start, 'repeats_per_profile': args.repeats, 'provider_calls': 0,
                  'provider_tokens': 0, 'download_calls': 0, 'asr_calls': 0},
              'denominators': {'independent_real_sources': 1, 'distinct_passages': 1,
                  'human_reviews': 0, 'render_samples': 0, 'evidence_samples': 0},
              'current_preview_profile': {'width': 360, 'height': 640, 'reference': 'clippie/collection_media.py:render_revision'},
              'limits': ['Deterministic transcript preprocessing, not model analysis.',
                  'Cold means fresh evidence artifact cache; OS/media caches are uncontrolled.',
                  'Repeated same-source passage is not an independent batch or four-source workload.',
                  'No quality, cost, cloud capacity, concurrency or ETA extrapolation; BATCH-02 remains partial.'],
              'evidence': [], 'renders': []}
    report_path = output / 'report.json'
    try:
        for repeat in range(args.repeats):
            cache = output / f'evidence-cache-{repeat+1}'
            for warmth in ('cold', 'warm'):
                evidence, usage = measure(lambda: source_evidence.prepare_evidence(source, cache))
                report['evidence'].append({'repeat': repeat+1, 'warmth': warmth,
                    'cache_status': evidence['cache_status'], 'artifact': str(evidence['path']),
                    'units': len(evidence['artifact']['units']), 'packets': len(evidence['artifact']['packets']), **usage})
                report['denominators']['evidence_samples'] += 1
        for width, height in ((360, 640), (540, 960)):
            timeline = copy.deepcopy(base)
            timeline.update(width=width, height=height)
            timeline = planner.validate_timeline(timeline, sources)
            for repeat in range(args.repeats):
                directory = output / f'{width}x{height}-{repeat+1}'
                directory.mkdir(exist_ok=False)
                result, usage = measure(lambda: render.render_production(timeline, sources, directory))
                paths = {key: str(result[key]) for key in ('video_path', 'srt_path', 'manifest_path')}
                probe = render._probe(Path(result['video_path']))
                manifest = json.loads(Path(result['manifest_path']).read_text())
                verified = (probe['has_audio'] and bool(probe.get('video_codec'))
                            and (probe['width'], probe['height']) == (width, height)
                            and abs(probe['duration']-(end-start)) <= max(.15, (end-start)*.01)
                            and manifest['metrics']['caption_words'] > 0
                            and Path(result['srt_path']).stat().st_size > 0)
                report['renders'].append({'repeat': repeat+1, 'profile': [width, height],
                    'timeline': manifest['timeline'], 'paths': paths, 'probe': probe,
                    'verified': bool(verified), 'video_bytes': Path(result['video_path']).stat().st_size,
                    'metrics': result['metrics'], 'warnings': result['warnings'], **usage})
                report['denominators']['render_samples'] += 1
                if not verified:
                    raise RuntimeError('Rendered output failed stream/geometry/duration/caption verification')
        after = media.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev) != (stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev):
            raise RuntimeError('Source stat identity changed during measurement')
        report['status'] = 'complete'
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
        print(report_path)


if __name__ == '__main__':
    main()
