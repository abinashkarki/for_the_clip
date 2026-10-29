"""Read-only legacy inventory; never copy source content into the report."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


def digest_file(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def inventory(root):
    root = Path(root).resolve()
    result = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
              'jobs': [], 'productions': [], 'files': {}}

    def files_in(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith('_path') and isinstance(item, str) and item:
                    path = Path(item)
                    if not path.is_absolute():
                        path = root / path
                    path = path.resolve()
                    name = str(path)
                    if name not in result['files']:
                        result['files'][name] = ({'size': path.stat().st_size, 'sha256': digest_file(path)}
                                                 if path.is_file() else {'missing': True})
                elif key not in {'source', 'source_info'}:
                    files_in(item)
        elif isinstance(value, list):
            for item in value:
                files_in(item)

    with sqlite3.connect((root / 'clippie.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')  # One consistent view of all legacy documents.
        for table in ('jobs', 'productions'):
            for record_id, raw in db.execute(f'SELECT id, document FROM {table} ORDER BY id'):
                document = json.loads(raw)
                entry = {'id': record_id, 'document_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                         'status': document.get('status')}
                if table == 'productions':
                    entry.update(revision=document.get('revision'), source_ids=document.get('source_ids', []),
                                 revision_count=len(document.get('revisions', [])))
                    renders = [document.get('render') or {}, *document.get('render_history', []),
                               *(r.get('render') or {} for r in document.get('revisions', []))]
                    entry['render_urls'] = sorted({f'/api/productions/{record_id}/video?render_id={r["id"]}'
                                                  for r in renders if r.get('id') and r.get('status') == 'ready'})
                result[table].append(entry)
                files_in(document)
    return result


def compare(before, after):
    """Allow additive work; report changed/deleted legacy records and artifacts."""
    issues = []
    for table in ('jobs', 'productions'):
        current = {row['id']: row for row in after[table]}
        for row in before[table]:
            if current.get(row['id']) != row:
                issues.append(f'{table}:{row["id"]}: changed or missing')
    for path, metadata in before['files'].items():
        if after['files'].get(path) != metadata:
            issues.append(f'artifact changed or missing: {path}')
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Choose a new output file; inventories are preserved.')
    report = inventory(args.data_dir)
    issues = compare(json.loads(args.compare.read_text()), report) if args.compare else []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'jobs': len(report['jobs']), 'productions': len(report['productions']),
                      'tracked_files': len(report['files']), 'legacy_changes': issues}))
    raise SystemExit(1 if issues else 0)


if __name__ == '__main__':
    main()
