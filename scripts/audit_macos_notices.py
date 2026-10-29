#!/usr/bin/env python3
"""Collect local notice evidence from an existing app; never a legal clearance.

Reads the bundle and recorded Homebrew provenance, executes only bundled media
binaries with -version/-L, and writes review artifacts outside the app.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'output/qa/mac-release-preflight-2026-10-27/notices'
SUPPLEMENT_ROOT = ROOT / 'packaging/macos/third-party'
LIMITATION = ('Evidence inventory only; owner review required. Wheel metadata does not '
              'establish complete transitive native compliance. Source delivery, license '
              'compatibility, attribution placement and redistribution rights remain unverified.')


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def notice_candidate(path):
    name = path.name.lower()
    return (name.startswith(('license', 'licence', 'copying', 'notice', 'copyright', 'ofl'))
            and path.suffix.lower() not in {'.py', '.pyc', '.so', '.dylib'}) or 'licenses' in path.parts


def inside(path, root):
    return path.resolve().is_relative_to(root.resolve())


def capture(binary, flag):
    try:
        result = subprocess.run([str(binary), flag], capture_output=True, text=True,
                                errors='replace', timeout=30, check=False)
        return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'error': str(exc)}


def app_cdhash(app):
    """Identify this signed resource seal; unsigned fixtures have no CDHash."""
    try:
        result = subprocess.run(['/usr/bin/codesign', '-d', '--verbose=4', str(app)],
                                capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return next((line.partition('=')[2].strip() for line in
                 (result.stdout + '\n' + result.stderr).splitlines() if line.startswith('CDHash=')), None)


def audit(app, output):
    app, output = app.resolve(), output.resolve()
    if inside(output, app):
        raise ValueError('Output must be outside the app bundle')
    resources = app / 'Contents/Resources'
    manifest_path = resources / 'build-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    findings, evidence, documents, recipient_documents = [], [], [], []

    def flag(code, subject, detail):
        findings.append({'code': code, 'subject': subject, 'detail': detail, 'status': 'owner_review'})

    def collect(path, origin='bundle'):
        key = (str(path), origin)
        if any((e['path'], e['origin']) == key for e in evidence):
            return str(path)
        raw = path.read_bytes()
        row = {'path': str(path), 'bundle_path': str(path.relative_to(app)) if inside(path, app) else None,
               'origin': origin, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        evidence.append(row)
        section = f"\n{'=' * 78}\n{origin}: {row['bundle_path'] or path}\nSHA256: {row['sha256']}\n{'=' * 78}\n" + raw.decode('utf-8', errors='replace')
        documents.append(section)
        recipient_documents.append(section)
        return str(path)

    files = [p for p in sorted(resources.rglob('*')) if p.is_file() and inside(p, app)]
    notice_files = [p for p in files if notice_candidate(p)]
    for path in notice_files:
        collect(path)
    supplements = json.loads((SUPPLEMENT_ROOT / 'provenance.json').read_text()) if (SUPPLEMENT_ROOT / 'provenance.json').is_file() else {'supplements': []}
    distributions, owners = [], {}
    for metadata in (p for p in files if p.name == 'METADATA' and p.parent.name.endswith('.dist-info')):
        dist = metadata.parent
        message = Parser().parsestr(metadata.read_text(errors='replace'))
        licenses = [str(p) for p in notice_files if p.is_relative_to(dist)]
        declared = message.get_all('License-File', [])
        missing = [name for name in declared if not any(
            (dist / base / name).is_file() and inside(dist / base / name, app) for base in ('', 'licenses'))]
        entry = {'name': message.get('Name', dist.name), 'version': message.get('Version'),
                 'metadata_path': str(metadata.relative_to(app)), 'metadata_sha256': sha256(metadata),
                 'license_expression': message.get('License-Expression'), 'license_field': message.get('License'),
                 'license_classifiers': [v for v in message.get_all('Classifier', []) if v.startswith('License ::')],
                 'declared_license_files': declared, 'missing_declared_license_files': missing,
                 'notice_files': licenses, 'home_page': message.get('Home-page'),
                 'project_urls': message.get_all('Project-URL', [])}
        entry['matching_supplements'] = []
        for supplement in supplements.get('supplements', []):
            if (supplement.get('name', '').lower().replace('_', '-') != entry['name'].lower().replace('_', '-')
                    or supplement.get('version') != entry['version']
                    or supplement.get('metadata_sha256') != entry['metadata_sha256']):
                continue
            path = SUPPLEMENT_ROOT / supplement['file']
            if not inside(path, SUPPLEMENT_ROOT) or not path.is_file() or sha256(path) != supplement.get('sha256'):
                flag('supplement_invalid', entry['name'], 'Matched supplement missing, outside root, or hash mismatch.')
                continue
            comparison = supplement.get('comparison_file')
            if comparison:
                proof_path = SUPPLEMENT_ROOT / comparison
                if not inside(proof_path, SUPPLEMENT_ROOT) or not proof_path.is_file() or sha256(proof_path) != supplement.get('comparison_sha256'):
                    flag('supplement_invalid', entry['name'], 'Supplement comparison proof missing or hash mismatch.')
                    continue
            collect(path, 'local-version-matched-supplement-not-shipped')
            entry['matching_supplements'].append(dict(supplement))
        direct = dist / 'direct_url.json'
        if direct.is_file():
            entry['direct_url'] = json.loads(direct.read_text())
        record = dist / 'RECORD'
        if record.is_file():
            for record_row in csv.reader(record.read_text().splitlines()):
                if record_row:
                    target = (dist.parent / record_row[0]).resolve()
                    if inside(target, app):
                        owners.setdefault(str(target), []).append(entry['name'])
        if not licenses and not entry['matching_supplements']:
            flag('distribution_notice_absent', entry['name'], 'No notice file found in distribution metadata directory; inspect package/vendored notices separately.')
        if missing:
            flag('declared_notice_missing', entry['name'], ', '.join(missing))
        if not entry['license_expression'] and not (entry['license_field'] or entry['license_classifiers']):
            flag('license_metadata_missing', entry['name'], 'No license expression, legacy license field or license classifier; inspect exact version evidence.')
        entry['license_metadata_basis'] = 'expression' if entry['license_expression'] else 'legacy metadata' if (entry['license_field'] or entry['license_classifiers']) else 'missing'
        distributions.append(entry)

    standalone_root = SUPPLEMENT_ROOT / 'standalone-python'
    standalone_provenance = standalone_root / 'provenance.json'
    if standalone_provenance.is_file():
        standalone = json.loads(standalone_provenance.read_text())
        build = resources / 'python/BUILD'
        if standalone.get('python_version') == manifest.get('python') and build.is_file() and build.read_text().strip() == standalone.get('pbs_release'):
            for filename, expected in standalone.get('license_files', {}).items():
                path = standalone_root / 'licenses' / filename
                if inside(path, standalone_root) and path.is_file() and sha256(path) == expected:
                    collect(path, 'local-version-matched-standalone-runtime-notice-not-shipped')
                else:
                    flag('standalone_notice_invalid', filename, 'Standalone notice missing or hash mismatch.')

    frontend_root = ROOT / 'clippie/static/studio'
    frontend_provenance = frontend_root / 'third-party-notices.provenance.json'
    frontend_notices = frontend_root / 'third-party-notices.txt'
    hanken_hashes = set()
    if frontend_provenance.is_file() and frontend_notices.is_file():
        frontend_data = json.loads(frontend_provenance.read_text())
        hanken = frontend_data.get('hanken_grotesk', {})
        if (frontend_data.get('notice_text_sha256') == sha256(frontend_notices)
                and hanken.get('emitted_assets_verified') is True):
            hanken_hashes = {entry.get('sha256') for entry in hanken.get('emitted_assets', [])
                              if entry.get('delivery') == 'file'}
            if any(sha256(p) in hanken_hashes for p in files if p.suffix.lower() == '.woff2'):
                collect(frontend_notices, 'local-hash-matched-frontend-notices-not-shipped')

    fonts = []
    for path in (p for p in files if p.suffix.lower() in {'.ttf', '.otf', '.woff', '.woff2'}):
        family = path.name.split('-')[0].lower()
        candidates = [str(p.relative_to(app)) for p in notice_files if family in p.name.lower()]
        if sha256(path) in hanken_hashes:
            candidates.append(str(frontend_notices))
        fonts.append({'path': str(path.relative_to(app)), 'sha256': sha256(path),
                      'candidate_notice_paths': candidates, 'association': 'hash matched frontend provenance' if sha256(path) in hanken_hashes else 'filename heuristic; owner verification required'})
        if not candidates:
            flag('font_notice_unmapped', str(path.relative_to(app)), 'No family-name notice match; trace font source and applicable license.')

    native = []
    provenance = manifest.get('ffmpeg', {})
    cellar_roots = set()
    for record in manifest.get('native_inventory', []):
        path = app / record['path']
        entry = dict(record)
        entry['wheel_owners'] = owners.get(str(path.resolve()), [])
        entry['bundled_sha256'] = sha256(path) if inside(path, app) and path.is_file() else None
        source = provenance.get(str(path.relative_to(resources))) if path.is_relative_to(resources) else None
        entry['build_source_provenance'] = source
        entry['license_mapping_status'] = ('wheel distribution notice association; vendored coverage not implied' if entry['wheel_owners'] else 'unmapped native component')
        if not entry['bundled_sha256']:
            flag('native_manifest_file_missing', record['path'], 'Native manifest entry absent or outside bundle.')
        if source:
            source_path = Path(source['source'])
            parts = source_path.parts
            if 'Cellar' in parts:
                index = parts.index('Cellar')
                if len(parts) > index + 2:
                    prefix = Path(*parts[:index + 3])
                    cellar_roots.add(prefix)
                    entry['source_package'] = parts[index + 1]
                    entry['source_package_version'] = parts[index + 2]
        native.append(entry)

    local_sources = []
    for prefix in sorted(cellar_roots):
        candidates = [p for p in prefix.glob('*') if p.is_file() and notice_candidate(p)]
        for directory in (prefix / 'share/licenses', prefix / 'share/doc'):
            if directory.is_dir():
                candidates.extend(p for p in directory.rglob('*') if p.is_file() and notice_candidate(p))
        notices = [collect(p, 'local-build-source-not-shipped') for p in sorted(set(candidates)) if inside(p, prefix)]
        source_record = {'prefix': str(prefix), 'notice_files': notices, 'metadata': []}
        for name in ('INSTALL_RECEIPT.json', 'sbom.spdx.json'):
            path = prefix / name
            if path.is_file() and inside(path, prefix):
                source_record['metadata'].append({'path': str(path), 'sha256': sha256(path)})
        local_sources.append(source_record)
        if not notices:
            flag('native_notice_missing', str(prefix), 'No license notice found in installed source package; locate exact-version notice.')

    media = {}
    for name in ('ffmpeg', 'ffprobe'):
        binary = resources / 'bin' / name
        media[name] = {flag_name: capture(binary, flag_name) if inside(binary, app) else {'error': 'outside bundle'}
                       for flag_name in ('-version', '-L')}
        for flag_name, result in media[name].items():
            documents.append(f'\n{name} {flag_name}\n' + json.dumps(result, indent=2))
            if result.get('error') or result.get('returncode') != 0:
                flag('media_probe_failed', name + ' ' + flag_name, 'Read probe failed; see captured output.')
    if (resources / 'bin/ffmpeg').is_file():
        flag('ffmpeg_source_delivery_review', 'FFmpeg and linked codec/native closure',
             'Review captured configuration and -L output, applicable GPL/LGPL/source delivery and relinking requirements, exact corresponding sources/build scripts and enabled libraries before redistribution. This collector does not verify recipient source delivery.')
    standalone_unmapped = [item['path'] for item in native if not item['wheel_owners'] and not item['build_source_provenance'] and item['path'].startswith('Contents/Resources/python/') ]
    if standalone_unmapped:
        flag('standalone_runtime_components_review', 'Standalone Python, Tcl/Tk and thread runtime',
             'Wheel RECORDs do not identify these runtime files; map exact standalone build notices and any source obligations: ' + ', '.join(standalone_unmapped))
    if any(item['wheel_owners'] and 'tokenizers' in item['wheel_owners'] for item in native):
        flag('tokenizers_native_vendored_review', 'tokenizers native Rust dependencies',
             "The exact top-level tokenizers notice is collected; map the Rust binary's vendored dependencies and their notices before assuming complete native coverage.")
    inventory = {'schema_version': 1, 'generated_at': datetime.now(timezone.utc).isoformat(), 'app': str(app),
                 'app_cdhash': app_cdhash(app),
                 'limitations': LIMITATION, 'redistribution_clearance': False, 'redistribution_review_complete': False, 'blockers': findings,
                 'build_manifest': {'path': str(manifest_path), 'sha256': sha256(manifest_path), 'record': manifest},
                 'python_distributions': distributions,
                 'standalone_python': {'version_from_manifest': manifest.get('python'),
                     'build_id': (resources / 'python/BUILD').read_text().strip() if (resources / 'python/BUILD').is_file() else None,
                     'notice_paths': [str(p.relative_to(app)) for p in notice_files if 'python' in p.parts and 'site-packages' not in p.parts]},
                 'app_resource_notice_paths': [str(p.relative_to(app)) for p in notice_files if p.is_relative_to(resources / 'app')],
                 'notice_evidence': evidence, 'fonts': fonts,
                 'native_components': native, 'local_source_packages': local_sources, 'media_probes': media,
                 'findings': findings}
    recipient = output / 'recipient-notices'
    recipient.mkdir(exist_ok=True)
    recipient_text = recipient / 'THIRD_PARTY_NOTICES.txt'
    recipient_text.write_text('Third-party licenses and notices for components bundled with Clippie.\n'
                              + '\n'.join(recipient_documents))
    notice_hash_by_path = {item['path']: item['sha256'] for item in evidence}
    resolution = {'schema_version': 1, 'app_cdhash': inventory['app_cdhash'],
                  'build_manifest_sha256': sha256(manifest_path),
                  'recipient_notices_sha256': sha256(recipient_text),
                  'limitations': 'Evidence mapping only; incomplete vendored coverage and source-delivery review may remain. Not legal clearance.',
                  'python_distributions': [{'name': d['name'], 'version': d['version'],
                      'metadata_sha256': d['metadata_sha256'], 'license_metadata_basis': d['license_metadata_basis'],
                      'notices': [{'path': p, 'sha256': notice_hash_by_path.get(p)} for p in d['notice_files']]
                          + [{'path': str(SUPPLEMENT_ROOT / x['file']), 'sha256': x['sha256'],
                              'association_strength': x.get('association_strength', 'exact version and metadata hash')}
                             for x in d['matching_supplements']]}
                     for d in distributions],
                  'native_components': [{'path': n['path'], 'sha256': n['bundled_sha256'],
                      'wheel_owners': n['wheel_owners'], 'source_package': n.get('source_package'),
                      'license_mapping_status': n['license_mapping_status']} for n in native],
                  'fonts': fonts, 'findings': findings}
    (recipient / 'notice-resolution-manifest.json').write_text(json.dumps(resolution, indent=2) + '\n')
    build_inputs = {'schema_version': 1,
                    'files': [{'path': 'THIRD_PARTY_NOTICES.txt', 'sha256': sha256(recipient_text)}],
                    'distributions': [{'name': d['name'], 'version': d['version'],
                        'metadata_path': d['metadata_path'], 'metadata_sha256': d['metadata_sha256']} for d in distributions],
                    'font_assets': [{'path': f['path'], 'sha256': f['sha256']} for f in fonts],
                    'scope': 'Input validation only. No app identity or redistribution clearance is asserted.'}
    (recipient / 'notice-inputs.json').write_text(json.dumps(build_inputs, indent=2) + '\n')
    (output / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
    (output / 'THIRD_PARTY_NOTICES.txt').write_text('Clippie third-party notice evidence — REVIEW DRAFT\n' + LIMITATION + '\n' + '\n'.join(documents))
    (output / 'review-findings.json').write_text(json.dumps(findings, indent=2) + '\n')
    return inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=ROOT / 'output/macos-spike/Clippie.app')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    inventory = audit(args.app, args.output)
    print(json.dumps({'output': str(args.output), 'distributions': len(inventory['python_distributions']),
                      'notice_files': len(inventory['notice_evidence']), 'native_components': len(inventory['native_components']),
                      'fonts': len(inventory['fonts']), 'review_findings': len(inventory['findings']),
                      'redistribution_clearance': False}))


if __name__ == '__main__':
    main()
