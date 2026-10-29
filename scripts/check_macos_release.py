#!/usr/bin/env python3
"""Read-only distribution preflight. A passing report is not beta acceptance.

No signing, credential access, notarization submission or publication is performed.
This checks a built app and optional installer against actual local signatures,
tickets and provenance. Human testing and redistribution decisions stay explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import subprocess


def command(arguments):
    try:
        result = subprocess.run([str(value) for value in arguments], capture_output=True,
                                text=True, timeout=45)
        return {'passed': result.returncode == 0, 'exit_code': result.returncode,
                'detail': (result.stdout + result.stderr)[-4000:]}
    except (OSError, subprocess.TimeoutExpired):
        return {'passed': False, 'exit_code': None, 'detail': 'Check unavailable or timed out; not verified.'}


def signature_details(output):
    values = {}
    for line in output.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values.setdefault(key, []).append(value)
    return {'developer_id': any(value.startswith('Developer ID Application:') for value in values.get('Authority', [])),
            'team': values.get('TeamIdentifier', [None])[0],
            'cdhash': values.get('CDHash', [None])[0],
            'timestamp': values.get('Timestamp', [None])[0],
            'hardened_runtime': bool(re.search(r'flags=0x[0-9a-f]+\([^)]*\bruntime\b', output))}


def collect(app, *, dmg=None, notices=None, expected_team=None, run=command):
    app = Path(app).resolve(strict=True)
    checks = []
    def check(identity, passed, detail):
        checks.append({'id': identity, 'passed': bool(passed), 'detail': detail})

    verified = run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    check('app_signature', verified['passed'], verified['detail'])
    details = run(['/usr/bin/codesign', '-d', '--verbose=4', app])
    signing = signature_details(details['detail'])
    check('developer_id', details['passed'] and signing['developer_id'],
          'Developer ID Application signature required for the distribution build.')
    check('secure_timestamp', details['passed'] and signing['timestamp'] not in {None, 'none'},
          'A secure signing Timestamp must be present.')
    check('hardened_runtime', details['passed'] and signing['hardened_runtime'],
          'Hardened runtime must be enabled on the app.')
    check('distribution_team', expected_team and signing['team'] == expected_team,
          'Actual signing team must match the owner-selected expected team.')
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    bundle = info.get('CFBundleIdentifier', '')
    check('production_bundle_id', isinstance(bundle, str) and '.' in bundle and not bundle.endswith('.spike'),
          'The current prototype identifier must be replaced by an owner-selected distribution identifier.')
    manifest = json.loads((app / 'Contents/Resources/build-manifest.json').read_text())
    check('clean_source', manifest.get('dirty_checkout') is False and bool(manifest.get('revision')),
          'Build provenance must identify a clean source commit.')
    check('native_inventory', bool(manifest.get('native_inventory')) and bool(manifest.get('native_minimum_macos')),
          'A native dependency inventory and derived OS floor must be recorded.')
    ticket = run(['/usr/bin/xcrun', 'stapler', 'validate', app])
    check('app_ticket', ticket['passed'], ticket['detail'])
    policy = run(['/usr/sbin/spctl', '--assess', '--type', 'execute', '--verbose=4', app])
    check('app_gatekeeper', policy['passed'], policy['detail'])
    if notices is not None:
        notice_bytes = Path(notices).read_bytes()
        audit = json.loads(notice_bytes)
        check('review_matches_app', bool(signing['cdhash']) and audit.get('app_cdhash') == signing['cdhash'],
              'The review inventory must identify this exact signed app resource seal.')
        check('redistribution_review', audit.get('schema_version') == 1
              and audit.get('redistribution_review_complete') is True and not audit.get('blockers'),
              f"Notice/source review has {len(audit.get('blockers', []))} unresolved findings. Collecting notices alone is not clearance.")
        notice_digest = hashlib.sha256(notice_bytes).hexdigest()
    else:
        check('redistribution_review', False, 'No notice/source review inventory supplied.')
        notice_digest = None
    if dmg is not None:
        dmg = Path(dmg).resolve(strict=True)
        with dmg.open('rb') as stream:
            dmg_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        signed = run(['/usr/bin/codesign', '--verify', '--strict', dmg])
        check('dmg_signature', signed['passed'], signed['detail'])
        description = run(['/usr/bin/codesign', '-d', '--verbose=4', dmg])
        dmg_signing = signature_details(description['detail'])
        check('dmg_developer_id', description['passed'] and dmg_signing['developer_id']
              and dmg_signing['team'] == expected_team and bool(expected_team)
              and dmg_signing['timestamp'] not in {None, 'none'},
              'The installer must have the selected Developer ID team and secure timestamp.')
        ticket = run(['/usr/bin/xcrun', 'stapler', 'validate', dmg])
        check('dmg_ticket', ticket['passed'], ticket['detail'])
        policy = run(['/usr/sbin/spctl', '--assess', '--type', 'open', '--context',
                      'context:primary-signature', '--verbose=4', dmg])
        check('dmg_gatekeeper', policy['passed'], policy['detail'])
    else:
        dmg_digest = None
        check('installer_supplied', False, 'No final installer was supplied.')
    return {'schema_version': 1, 'preflight_passed': all(item['passed'] for item in checks),
            'independent_beta_acceptance': False, 'app': str(app),
            'bundle_id': bundle, 'source_revision': manifest.get('revision'),
            'native_minimum_macos': manifest.get('native_minimum_macos'),
            'installer_sha256': dmg_digest, 'notices_sha256': notice_digest,
            'checks': checks,
            'remaining_acceptance': ['Downloaded/quarantined exact artifact on clean supported Macs',
                'Installer contents match the selected app and recipient receives notices/source material',
                'Live Keychain/provider, model setup and project upgrade/restore acceptance',
                'Human audio, framing, caption and editorial acceptance']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('--dmg', type=Path)
    parser.add_argument('--notices', type=Path)
    parser.add_argument('--expected-team')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = collect(args.app, dmg=args.dmg, notices=args.notices, expected_team=args.expected_team)
    except (OSError, ValueError, KeyError, TypeError):
        parser.error('Cannot read the selected app, manifest or review inventory. No release check passed.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'preflight_passed': report['preflight_passed'],
                      'failed_checks': [item['id'] for item in report['checks'] if not item['passed']]}))
    return 0 if report['preflight_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
