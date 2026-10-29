#!/usr/bin/env python3
"""Verify a downloaded DMG and its already-mounted contents, without changing it.

The trusted expected hash must be obtained independently. This is a mechanical
inspection, not proof of a successful quarantined Finder installation.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import plistlib
import subprocess
from pathlib import Path
from check_macos_close_testers import checked_file
from check_macos_release import command, signature_details


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def mounted_image_matches(dmg, mount):
    try:
        info = subprocess.run(['/usr/bin/hdiutil', 'info', '-plist'], capture_output=True, check=True, timeout=30)
        images = plistlib.loads(info.stdout).get('images', [])
        return any(Path(image.get('image-path', '')).resolve() == dmg and any(
            entity.get('mount-point') and Path(entity['mount-point']).resolve() == mount
            for entity in image.get('system-entities', [])) for image in images)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return False


def inspect(dmg, mount, sidecar, expected_sha256, separate=None, *, run=command, quarantine=None, mount_check=mounted_image_matches):
    dmg, mount = Path(dmg).resolve(strict=True), Path(mount).resolve(strict=True)
    record = json.loads(Path(sidecar).read_text())
    if not isinstance(record, dict) or not isinstance(record.get('staged_delivery_files'), list):
        raise ValueError('Invalid installer sidecar.')
    checks = []
    def check(name, ok, detail):
        checks.append({'id': name, 'passed': bool(ok), 'detail': detail})
    actual = digest(dmg)
    check('download_hash', actual == expected_sha256 and actual == record.get('sha256'),
          'Downloaded DMG matches both independently supplied SHA-256 and the installer record.')
    if quarantine is None:
        try:
            result = subprocess.run(['/usr/bin/xattr', '-p', 'com.apple.quarantine', str(dmg)],
                                    capture_output=True, timeout=10)
            quarantine = result.returncode == 0 and bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            quarantine = False
    check('download_quarantined', quarantine, 'The downloaded DMG has a macOS quarantine attribute; Finder installation still needs observation.')
    check('mounted_image', mount_check(dmg, mount), 'The mounted volume belongs to the exact downloaded DMG.')
    app = mount / 'Clippie.app'
    signature = run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    desc = run(['/usr/bin/codesign', '-d', '--verbose=4', app])
    cdhash = signature_details(desc['detail'])['cdhash'] if desc['passed'] else None
    check('mounted_app', signature['passed'] and cdhash and cdhash == record.get('app_cdhash'),
          'The mounted app signature and CDHash match the installer record.')
    seen, files_ok = set(), True
    image_paths = set()
    for entry in record['staged_delivery_files']:
        if not isinstance(entry, dict) or entry.get('placement') not in {'image', 'separate'}:
            files_ok = False
            break
        root = mount if entry['placement'] == 'image' else (Path(separate).resolve() if separate else None)
        name = entry.get('path')
        if root is None or not isinstance(name, str) or (entry['placement'], name) in seen:
            files_ok = False
            break
        seen.add((entry['placement'], name))
        file = checked_file(root, name)
        if (file is None or not isinstance(entry.get('sha256'), str)
                or file.stat().st_size != entry.get('bytes') or digest(file) != entry['sha256']):
            files_ok = False
            break
        if entry['placement'] == 'image':
            image_paths.add(name)
    delivery_dir = mount / 'Notices and Source Information'
    if delivery_dir.is_dir():
        actual_paths = {str(p.relative_to(mount)) for p in delivery_dir.rglob('*') if p.is_file() or p.is_symlink()}
    else:
        actual_paths = set()
    check('delivery_contents', bool(record['staged_delivery_files']) and files_ok and actual_paths == image_paths,
          'All recorded delivery files match their hashes, including separately supplied files, and no unrecorded image delivery files appear.')
    readme = mount / 'READ BEFORE USE.txt'
    check('unnotarized_warning', readme.is_file() and 'UNNOTARIZED' in readme.read_text(errors='replace'),
          'The mounted image displays the unnotarized warning.')
    return {'mechanical_inspection_passed': all(c['passed'] for c in checks), 'dmg_sha256': actual,
            'app_cdhash': cdhash, 'checks': checks, 'independent_install_accepted': False,
            'remaining': 'Observe Finder installation and first launch on a clean supported Mac, verify recipient access to any separate source, and record setup, export, recovery and quality.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dmg', type=Path, required=True)
    parser.add_argument('--mounted', type=Path, required=True)
    parser.add_argument('--sidecar', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--separate-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = inspect(args.dmg, args.mounted, args.sidecar, args.expected_sha256, args.separate_dir)
    except (OSError, ValueError, TypeError, KeyError):
        parser.error('Cannot inspect selected installer or evidence.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'mechanical_inspection_passed': report['mechanical_inspection_passed'],
                      'failed_checks': [c['id'] for c in report['checks'] if not c['passed']]}))
    return 0 if report['mechanical_inspection_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
