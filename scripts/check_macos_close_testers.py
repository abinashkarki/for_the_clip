#!/usr/bin/env python3
"""Read-only assisted-alpha preflight; does not create or approve an installer.

This is distinct from the Developer ID release preflight. A passing report is
only mechanical evidence; the exact downloaded installer still needs testing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
import zipfile
from pathlib import Path

from check_macos_release import command, signature_details


def checked_file(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return None
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_relative_to(root.resolve()) and resolved.is_file() else None


def source_archive_has_files(path):
    """Check that corresponding-source delivery is an actual nonempty archive."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                return any(not item.is_dir() for item in archive.infolist())
        with tarfile.open(path, 'r:*') as archive:
            return any(item.isfile() for item in archive)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError):
        return False


def collect(app, *, review=None, delivery=None, run=command):
    app = Path(app).resolve(strict=True)
    manifest = json.loads((app / 'Contents/Resources/build-manifest.json').read_text())
    checks = []

    def check(name, passed, detail):
        checks.append({'id': name, 'passed': bool(passed), 'detail': detail})

    verified = run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    check('app_signature', verified['passed'], verified['detail'])
    details = run(['/usr/bin/codesign', '-d', '--verbose=4', app])
    cdhash = signature_details(details['detail'])['cdhash'] if details['passed'] else None
    check('app_identity', bool(cdhash), 'A CodeDirectory hash must identify the verified app.')
    required = ('Contents/MacOS/Clippie', 'Contents/Resources/python/bin/python3',
                'Contents/Resources/bin/ffmpeg', 'Contents/Resources/bin/ffprobe')
    runtime_ok = all((p := checked_file(app, name)) is not None and os.access(p, os.X_OK) for name in required)
    inventory = manifest.get('native_inventory')
    inventory_ok = isinstance(inventory, list) and bool(inventory) and all(
        isinstance(row, dict) and checked_file(app, row.get('path')) is not None for row in inventory)
    check('owned_runtime', runtime_ok and inventory_ok and bool(manifest.get('native_minimum_macos')),
          'Executable launcher, Python and media tools and all inventoried native files must resolve inside the app; OS floor must be recorded. Runtime behavior and dependency closure need separate testing.')
    check('source_provenance', bool(manifest.get('revision')) and isinstance(manifest.get('dirty_checkout'), bool),
          'Record the source revision and whether the build had uncommitted changes; retain the actual source snapshot separately.')
    review_data = json.loads(Path(review).read_text()) if review else {}
    if not isinstance(review_data, dict):
        raise ValueError('Review inventory must be a JSON object.')
    check('review_matches_app', bool(cdhash) and review_data.get('app_cdhash') == cdhash,
          'The redistribution review must identify this exact app CodeDirectory hash.')
    check('redistribution_review', review_data.get('schema_version') == 1
          and review_data.get('technical_materials_review_complete') is True
          and review_data.get('blockers') == [],
          'App-bound technical notice and source review must be complete with no unresolved blockers. This is not legal certification; private sharing is redistribution.')
    delivery_data = json.loads(Path(delivery).read_text()) if delivery else {}
    if not isinstance(delivery_data, dict):
        raise ValueError('Delivery manifest must be a JSON object.')
    root = Path(delivery).resolve().parent if delivery else None
    items = delivery_data.get('files')
    files_ok = isinstance(items, list) and bool(items)
    kinds = set()
    if files_ok:
        for item in items:
            if not isinstance(item, dict) or item.get('kind') not in {'notice', 'source', 'source_evidence'} or item.get('placement', 'image') not in {'image', 'separate'}:
                files_ok = False
                break
            path = checked_file(root, item.get('path'))
            if not path or not isinstance(item.get('sha256'), str):
                files_ok = False
                break
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                    files_ok = False
                    break
            if path.stat().st_size == 0 or (item['kind'] == 'source' and not source_archive_has_files(path)):
                files_ok = False
                break
            kinds.add(item['kind'])
    check('delivery_evidence', delivery_data.get('schema_version') == 1
          and delivery_data.get('app_cdhash') == cdhash and bool(cdhash)
          and delivery_data.get('source_obligations_reviewed') is True
          and files_ok and kinds == {'notice', 'source', 'source_evidence'},
          'Provide nonempty hash-verified notices, a readable corresponding-source archive containing files, and source-obligation evidence bound to this app. Confirm the actual required materials reach every recipient; this check cannot establish legal sufficiency.')
    requirements = review_data.get('delivery_requirements')
    delivered = {item.get('path') for item in items if isinstance(item, dict)} if isinstance(items, list) else set()
    mapped = set()
    mapping_ok = isinstance(requirements, list) and bool(requirements)
    if mapping_ok:
        for row in requirements:
            if (not isinstance(row, dict) or not isinstance(row.get('component'), str)
                    or not row['component'].strip() or not isinstance(row.get('files'), list)
                    or not row['files'] or any(not isinstance(p, str) or p not in delivered for p in row['files'])):
                mapping_ok = False
                break
            mapped.update(row['files'])
    check('reviewed_delivery_mapping', mapping_ok and mapped == delivered,
          'The completed app-bound review must map each delivered file to reviewed components and obligations. Mapping and hashes are evidence, not an automated legal determination.')
    return {'schema_version': 1, 'assisted_alpha_preflight_passed': all(c['passed'] for c in checks),
            'independent_install_accepted': False, 'app': str(app), 'app_cdhash': cdhash,
            'source_revision': manifest.get('revision'), 'dirty_checkout': manifest.get('dirty_checkout'),
            'native_minimum_macos': manifest.get('native_minimum_macos'), 'checks': checks,
            'warning': 'Assisted alpha: unnotarized; macOS may block opening and managed Macs may prohibit exceptions.',
            'remaining_acceptance': ['Verify final installer contents, notices and applicable source delivery',
                'Test the exact downloaded and quarantined installer on supported clean Macs',
                'Verify first launch, setup, recovery, export and human quality within an agreed provider allowance']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--delivery', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = collect(args.app, review=args.review, delivery=args.delivery)
    except (OSError, ValueError, KeyError, TypeError):
        parser.error('Cannot read selected app or evidence. No assisted-alpha check passed.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'assisted_alpha_preflight_passed': report['assisted_alpha_preflight_passed'],
                      'failed_checks': [c['id'] for c in report['checks'] if not c['passed']]}))
    return 0 if report['assisted_alpha_preflight_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
