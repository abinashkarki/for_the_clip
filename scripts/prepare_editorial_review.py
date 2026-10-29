"""Prepare unpaid, deliberately defective QA videos for pending human listening review."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path,
                        default=ROOT / 'docs/archive/examples/temple-negative-editorial-fixtures.json')
    parser.add_argument('--output', type=Path, required=True,
                        help='Required new isolated directory outside repository/configured data; never overwritten')
    args = parser.parse_args()
    from clippie import planner, render, source_evidence

    output = args.output.expanduser().absolute()
    require(not output.exists() and not output.is_symlink(), 'Output must be a new directory')
    output = output.resolve()
    data = Path(os.getenv('CLIPPIE_DATA_DIR', ROOT / 'data')).expanduser().resolve()
    for forbidden in {data, (ROOT / 'data').resolve()}:
        require(not output.is_relative_to(forbidden), 'Output cannot be inside data')
    fixture_path = args.fixtures.expanduser().resolve()
    fixtures = read(fixture_path)
    evidence_path = Path(fixtures['source_evidence_path']).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = ROOT / evidence_path
    evidence_path = evidence_path.resolve()
    require(sha(evidence_path) == fixtures['source_evidence_sha256'], 'Evidence SHA mismatch')
    evidence = read(evidence_path)
    sid = fixtures['source_id']
    require(evidence['manifest']['source_id'] == sid, 'Evidence source mismatch')
    require(evidence['manifest']['transcript_hash'] == fixtures['transcript_hash'], 'Transcript hash mismatch')
    # Do not instantiate Store: its constructor/schema setup writes to the database.
    database = data / 'clippie.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT document FROM jobs WHERE id=?', (sid,)).fetchone()
    require(row is not None, 'Source absent from read-only source store')
    source = json.loads(row[0])
    require(source['id'] == sid, 'Stored source ID mismatch')
    source_path = Path(source['source_path']).expanduser().resolve(strict=True)
    payload = source_evidence._payload(source, evidence['manifest']['max_chunk_chars'])
    current = source_evidence._build(payload, source_evidence._hash(payload), payload['max_chunk_chars'])
    require(current['manifest']['transcript_hash'] == fixtures['transcript_hash'], 'Current transcript changed')
    require(current['units'] == evidence['units'], 'Current evidence units changed')
    units = {u['id']: u for u in current['units']}
    words = {w['id']: w for w in current['transcript']['words']}
    media_sha = sha(source_path)
    baseline_path = ROOT / 'output/qa/collection-real01-fixture-20261023.json'
    baseline_check = 'unavailable'
    if baseline_path.exists():
        baseline = read(baseline_path)
        require(baseline['source_id'] == sid and baseline['source_sha256'] == media_sha,
                'Frozen REAL-01 source identity/SHA mismatch')
        baseline_check = 'matched'
    sources = {sid: dict(source, source_path=str(source_path), words=current['transcript']['words'])}
    cases, ids, excerpt_count = [], set(), 0
    require(0 < len(fixtures['fixtures']) <= 3, 'Expected one to three bounded fixtures')
    for fixture in fixtures['fixtures']:
        fid = fixture['id']
        require(re.fullmatch(r'[A-Za-z0-9_-]+', fid) and fid not in ids, 'Unsafe/duplicate fixture ID')
        ids.add(fid)
        require(fixture['label_status'] == 'model_proposed_not_human_verified', 'Label must remain proposed')
        require(fixture['expected_layer'] in {'deterministic_integrity', 'editorial_review'}, 'Unknown layer')
        require(fixture['context'] and fixture['output_order'], 'Empty fixture sequence')
        context_ids = {x['unit_id'] for x in fixture['context']}
        for excerpt in fixture['context'] + fixture['output_order']:
            unit = units[excerpt['unit_id']]
            selected = excerpt['word_ids']
            require(bool(selected), 'Empty word range')
            offset = unit['word_ids'].index(selected[0])
            require(selected == unit['word_ids'][offset:offset + len(selected)], 'Noncontiguous word range')
            require(excerpt['quote'] == ' '.join(words[i]['text'] for i in selected), 'Quote mismatch')
            require(excerpt['start'] == words[selected[0]]['start'] and
                    excerpt['end'] == words[selected[-1]]['end'], 'Word endpoint mismatch')
            require(all(math.isfinite(excerpt[k]) for k in ('start', 'end')), 'Nonfinite endpoint')
            excerpt_count += 1
        require(all(x['unit_id'] in context_ids for x in fixture['output_order']), 'Output outside context')
        spans = {'context': [(min(x['start'] for x in fixture['context']),
                              max(x['end'] for x in fixture['context']))],
                 'negative': [(x['start'], x['end']) for x in fixture['output_order']]}
        timelines = {}
        for kind, ranges in spans.items():
            require(all(0 <= start < end <= source['duration'] for start, end in ranges), 'Invalid source bounds')
            require(sum(end - start for start, end in ranges) <= 180, 'Review video exceeds 180 seconds')
            timeline = {'schema_version': 2, 'aspect_ratio': '9:16', 'width': 360, 'height': 640,
                        'fps': 30, 'captions': True, 'caption_style': 'editorial', 'audio': {'normalize': True},
                        'clips': [{'id': f'qa-{n}', 'source_id': sid, 'start': start, 'end': end,
                                   'framing': {'mode': 'fit'}} for n, (start, end) in enumerate(ranges)]}
            timelines[kind] = planner.validate_timeline(timeline, sources)
        cases.append({'fixture': fixture, 'timelines': timelines, 'artifacts': {}})
    output.mkdir(parents=True, exist_ok=False)  # All input checks precede rendering/files.
    manifest = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
                'purpose': 'Deliberate QA negative edits; never generated production successes',
                'human_review': 'pending', 'human_acceptance': 'unknown', 'provider_calls': 0,
                'fixtures_path': str(fixture_path), 'fixtures_sha256': sha(fixture_path),
                'evidence_path': str(evidence_path), 'evidence_sha256': sha(evidence_path),
                'transcript_hash': fixtures['transcript_hash'], 'source_id': sid,
                'source_store': str(database), 'source_path': str(source_path), 'source_sha256': media_sha,
                'frozen_source_check': baseline_check, 'validated_excerpts': excerpt_count, 'cases': cases}
    for case in cases:
        for kind, timeline in case['timelines'].items():
            directory = output / case['fixture']['id'] / kind
            result = render.render_production(timeline, sources, directory)
            files = {key: {'path': str(Path(result[key]).relative_to(output)), 'sha256': sha(result[key])}
                     for key in ('video_path', 'srt_path', 'manifest_path')}
            case['artifacts'][kind] = {'files': files, 'probe': render._probe(Path(result['video_path'])),
                                       'metrics': result['metrics'], 'warnings': result['warnings']}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    escape = lambda value: html.escape(str(value), quote=True)
    sections = []
    for case in cases:
        fixture = case['fixture']
        layer_note = ('Collection compiler whole-unit policy should reject this partial-unit selection before model review. '
                      'This QA render deliberately bypasses that collection gate to make the proposed defect audible.'
                      if fixture['expected_layer'] == 'deterministic_integrity' else
                      'Proposed editorial-review case; detector performance has not been measured.')
        players = []
        for kind, label, quotes in [('context', 'Contiguous original context', fixture['context']),
                                    ('negative', 'Deliberate defective edit', fixture['output_order'])]:
            url = case['artifacts'][kind]['files']['video_path']['path']
            players.append(f'<div><h3>{label}</h3><video controls preload="metadata" src="{escape(url)}"></video>'
                           + ''.join(f'<p><code>{escape(q["unit_id"])} [{q["start"]}–{q["end"]}]</code><br>'
                                     f'{escape(q["quote"])}</p>' for q in quotes) + '</div>')
        sections.append(f'<section><h2>{escape(fixture["id"])} · {escape(fixture["proposed_defect"])}</h2>'
                        f'<p>Proposed severity: {escape(fixture["proposed_severity"])}; '
                        f'{escape(fixture["label_status"])}. Expected layer: {escape(fixture["expected_layer"])}</p>'
                        f'<p>{escape(fixture["explanation"])}</p><p>{escape(layer_note)}</p>'
                        f'<div class="pair">{"".join(players)}</div></section>')
    page = ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>Pending editorial QA listening review</title>'
            '<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 18px;color:#18202b}'
            '.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}video{width:100%;max-height:520px;'
            'background:#111}section{border-top:1px solid #aaa;padding:20px 0}code{overflow-wrap:anywhere}'
            '@media(max-width:650px){.pair{grid-template-columns:1fr}}</style><h1>Pending editorial QA listening review</h1>'
            '<p>These are deliberately defective QA edits, not generated production outputs or accepted human labels. '
            'Compare original audio/context with each negative edit. Human review remains pending; no decisions are saved here.</p>'
            '<p>ASR words and approximate alignment are unverified; speakers are unknown. Source claims have not been fact checked. '
            'Captions reproduce cached ASR, including recognition errors. Listening is required before confirming any label.</p>'
            + ''.join(sections) + '<p>Unprepared categories: '
            + escape(', '.join(x['category'] for x in fixtures['missing_categories']))
            + '.</p><p><a href="manifest.json">Exact inputs, timelines, hashes and media probes</a></p></html>')
    (output / 'index.html').write_text(page)
    print(json.dumps({'output': str(output), 'cases': len(cases), 'validated_excerpts': excerpt_count,
                      'human_review': 'pending', 'source_sha_check': baseline_check}))


if __name__ == '__main__':
    main()
