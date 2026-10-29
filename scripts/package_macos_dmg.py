#!/usr/bin/env python3
"""Create a local-spike DMG or a gated unnotarized assisted-alpha image.

This does not notarize, publish, or grant redistribution clearance. The alpha
mode requires a completed app-bound review and staged delivery evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import shutil
import unicodedata


def detach_creation_image(output):
    """Newer macOS may leave create's unmounted write-once driver attached.

    Eject only the image this command just created, owned by this user and with
    no mounted volume. Never eject another image or an in-use mounted volume.
    """
    info = subprocess.run(['/usr/bin/hdiutil', 'info', '-plist'], capture_output=True, check=True)
    for image in plistlib.loads(info.stdout).get('images', []):
        if image.get('image-path') != str(output) or image.get('owner-uid') != os.getuid():
            continue
        entities = image.get('system-entities', [])
        if any(entity.get('mount-point') for entity in entities):
            raise RuntimeError('The new disk image has a mounted volume. Close it before verifying the installer.')
        if image.get('system-image') is not True or image.get('writeable') is not True:
            raise RuntimeError('The image is in use outside its creation driver. Close it before verifying the installer.')
        device = next((entity.get('dev-entry', '') for entity in entities
                       if entity.get('content-hint') == 'GUID_partition_scheme'), '')
        if not re.fullmatch(r'/dev/disk\d+', device):
            raise RuntimeError('Could not identify the newly created image driver safely.')
        subprocess.run(['/usr/sbin/diskutil', 'eject', device], check=True)


def stage_close_tester_materials(app, review, delivery, staging, *, run=None):
    """Recheck the selected app, then copy and hash the exact delivery files."""
    from check_macos_close_testers import collect
    review_bytes = Path(review).read_bytes()
    delivery_bytes = Path(delivery).read_bytes()
    options = {'review': review, 'delivery': delivery}
    if run is not None:
        options['run'] = run
    report = collect(app, **options)
    if not report['assisted_alpha_preflight_passed']:
        failed = ', '.join(c['id'] for c in report['checks'] if not c['passed'])
        raise ValueError('Assisted-alpha preflight failed: ' + failed)
    if Path(review).read_bytes() != review_bytes or Path(delivery).read_bytes() != delivery_bytes:
        raise ValueError('Review or delivery manifest changed during preflight.')
    report['review_sha256'] = hashlib.sha256(review_bytes).hexdigest()
    report['delivery_manifest_sha256'] = hashlib.sha256(delivery_bytes).hexdigest()
    data = json.loads(delivery_bytes)
    root = Path(delivery).resolve().parent
    target = Path(staging) / 'Notices and Source Information'
    target.mkdir()
    records = []
    seen = set()
    for item in data['files']:
        name = item['path']
        path = Path(name)
        normalized = unicodedata.normalize('NFC', name).casefold()
        if (path.is_absolute() or not path.parts or name != path.as_posix() or any(part in {'.', '..'} or part.endswith((' ', '.')) for part in name.split('/'))
                or '\\' in name or normalized in seen):
            raise ValueError('Delivery paths must be unique, normalized relative paths.')
        seen.add(normalized)
        source = (root / path).resolve(strict=True)
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError('Delivery file must resolve inside the evidence folder.')
        placement = item.get('placement', 'image')
        if placement == 'separate':
            dest = source
            record_path = name
        else:
            dest = target / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            record_path = str(dest.relative_to(staging))
        with dest.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != item['sha256']:
            raise ValueError('Delivery file changed during staging.')
        records.append({'kind': item['kind'], 'placement': placement, 'path': record_path, 'sha256': actual,
                        'bytes': dest.stat().st_size})
    return report, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--close-testers', action='store_true', help='Create a gated unnotarized assisted-alpha image.')
    parser.add_argument('--review', type=Path, help='Completed app-bound redistribution review.')
    parser.add_argument('--delivery', type=Path, help='App-bound delivery manifest and files.')
    args = parser.parse_args()
    app = args.app.resolve(strict=True)
    setup_available = (app / 'Contents/Resources/app/clippie/model_setup.py').is_file()
    manifest = json.loads((app / 'Contents/Resources/build-manifest.json').read_text())
    if manifest.get('notarized') is not False or manifest.get('redistribution_review_complete') is not False:
        parser.error('Use the release pipeline for a distribution-approved app; this creates only a labeled local spike.')
    if args.close_testers and (not args.review or not args.delivery):
        parser.error('Assisted alpha requires completed review and delivery evidence.')
    if not args.close_testers and (args.review or args.delivery):
        parser.error('Review and delivery are only used with --close-testers.')
    default_name = 'Clippie-assisted-alpha.dmg' if args.close_testers else 'Clippie-runtime-spike.dmg'
    output = (args.output or app.parent / default_name).resolve()
    if output.exists():
        parser.error('Output already exists. Choose a new filename or explicitly remove the obsolete local artifact.')
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    with tempfile.TemporaryDirectory(prefix='clippie-dmg-') as directory:
        staging = Path(directory)
        subprocess.run(['/bin/cp', '-cR', str(app), str(staging / 'Clippie.app')], check=True)
        (staging / 'Applications').symlink_to('/Applications')
        delivery_records = []
        if args.close_testers:
            try:
                report, delivery_records = stage_close_tester_materials(staging / 'Clippie.app', args.review, args.delivery, staging)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                parser.error('Cannot stage assisted alpha: ' + str(exc))
            label = 'Clippie Assisted Alpha'
            (staging / 'READ BEFORE USE.txt').write_text(
                'CLIPPIE PRIVATE ASSISTED ALPHA — UNNOTARIZED\n\n'
                'Apple has not notarized this app or checked it for known malware. '
                'macOS may block it, and managed Macs may prohibit exceptions. '
                'Only install if you trust the sender and have confirmed the checksum through a trusted channel.\n'
                'Drag Clippie into Applications and open it. If macOS blocks the app as unnotarized '
                'and you independently trust it, Apple describes the per-app Open Anyway option at '
                'https://support.apple.com/en-au/102445. Stop and contact the owner if the app is '
                'reported damaged or malicious.\n'
                'Review the materials in Notices and Source Information and any separate source bundle supplied by the owner. Confirm support and provider '
                'spending allowance with the owner. Models may need separate download and terms review.\n'
                f'Native minimum macOS for this exact artifact: {manifest["native_minimum_macos"]}.\n'
                'Downloaded installation and independent device acceptance remain to be tested.\n')
        else:
            label = 'Clippie Runtime Spike'
            (staging / 'READ BEFORE USE.txt').write_text(
                'CLIPPIE LOCAL RUNTIME SPIKE\n\n'
                'This is an internal packaging feasibility build, not an independent beta.\n'
                'It is not notarized or cleared for external redistribution.\n' +
                ('Model weights are not included. Use Settings to review publisher terms and prepare them.\n'
                 if setup_available else 'Models and first-run provisioning are not included.\n') +
                'The app uses Application Support for its data. Independent clean-Mac acceptance and upgrades remain unverified.\n'
                f'Native minimum macOS for this exact artifact: {manifest["native_minimum_macos"]}.\n'
                'See the repository packaging report for acceptance evidence and remaining gates.\n')
        subprocess.run(['/usr/bin/hdiutil', 'create', '-volname', label, '-srcfolder', str(staging),
                        '-format', 'UDZO', str(output)], check=True)
    detach_creation_image(output)
    subprocess.run(['/usr/bin/hdiutil', 'verify', str(output)], check=True)
    with output.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    record = {'artifact': output.name, 'bytes': output.stat().st_size, 'sha256': checksum,
              'purpose': 'private assisted alpha' if args.close_testers else 'local runtime spike',
              'notarized': False, 'redistribution_review_complete': False,
              'technical_materials_review_complete': bool(args.close_testers), 'legal_certification': False,
              'app_cdhash': report['app_cdhash'] if args.close_testers else None,
              'staged_delivery_files': delivery_records,
              'review_sha256': report['review_sha256'] if args.close_testers else None,
              'delivery_manifest_sha256': report['delivery_manifest_sha256'] if args.close_testers else None,
              'model_setup_available': setup_available,
              'native_minimum_macos': manifest['native_minimum_macos']}
    output.with_suffix('.dmg.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
