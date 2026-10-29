#!/usr/bin/env python3
"""Reconcile candidate-bound notice/source evidence for an assisted alpha.

Outputs an owner-review draft and actual recipient files; it does not decide
redistribution rights or prove that a downloaded installer works.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile

from check_macos_release import command, signature_details

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def inside_file(root, path):
    if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts:
        raise ValueError('Invalid relative path')
    result = (root / path).resolve(strict=True)
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError('Evidence file missing or outside its root: ' + path)
    return result


def validate_media_archive(path, ffmpeg):
    """Verify each listed source member and the exact original build inputs."""
    with tarfile.open(path, 'r:*') as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(names) != len(set(names)) or any(not m.isfile() for m in members):
            raise ValueError('Unexpected or duplicate media archive members')
        sums = archive.extractfile('SHA256SUMS').read().decode()
        expected = {}
        for line in sums.splitlines():
            sha, sep, name = line.partition('  ')
            if not sep or len(sha) != 64 or name in expected or name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('Invalid media source checksum listing')
            expected[name] = sha
        if set(expected) != set(names) - {'SHA256SUMS'}:
            raise ValueError('Media source archive listing does not match members')
        for name, sha in expected.items():
            stream = archive.extractfile(name)
            if hashlib.file_digest(stream, 'sha256').hexdigest() != sha:
                raise ValueError('Media source member changed: ' + name)
        prior = json.load(archive.extractfile('app-build-manifest.json'))
        if not ffmpeg or ffmpeg != prior.get('ffmpeg'):
            raise ValueError('Candidate media inputs differ from source archive build inputs')
        pins_bytes = archive.extractfile('source-pins.json').read()
        if hashlib.sha256(pins_bytes).hexdigest() != digest(ROOT / 'packaging/macos/third-party/source-inputs.json'):
            raise ValueError('Media source pins differ from reviewed pins')
        pins = json.loads(pins_bytes)
        for item in pins['downloads']:
            if expected.get(item['filename']) != item['sha256']:
                raise ValueError('Media source member does not match pinned source: ' + item['filename'])
        collection = json.load(archive.extractfile('collection.json'))
        x264 = collection['x264']
        if expected.get(x264['file']) != x264['sha256'] or x264['status'] not in {'verified_git_commit', 'verified_cached'}:
            raise ValueError('x264 source evidence is incomplete')
    return {'sha256': digest(path), 'members': len(expected), 'media_inputs_match': True}


def prepare(app, audit_path, notice_inputs, media_archive, standalone_dir, standalone_sources, output, *, run=command):
    app, output = Path(app).resolve(strict=True), Path(output).resolve()
    if output.is_relative_to(app):
        raise ValueError('Output must be outside app')
    audit = json.loads(Path(audit_path).read_text())
    manifest_path = app / 'Contents/Resources/build-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    notice_inputs = Path(notice_inputs)
    inputs = json.loads(notice_inputs.read_text())
    verified = run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    desc = run(['/usr/bin/codesign', '-d', '--verbose=4', app])
    cdhash = signature_details(desc['detail'])['cdhash'] if desc['passed'] else None
    if not verified['passed'] or not cdhash or audit.get('app_cdhash') != cdhash or audit.get('build_manifest', {}).get('sha256') != digest(manifest_path):
        raise ValueError('Audit or intact signature does not match candidate')
    if inputs.get('schema_version') != 1 or not inputs.get('files') or not inputs.get('distributions'):
        raise ValueError('Notice inputs are incomplete')
    expected_dist = {(d['metadata_path'], d['metadata_sha256']) for d in inputs['distributions']}
    audited_dist = {(d['metadata_path'], d['metadata_sha256']) for d in audit.get('python_distributions', [])}
    if len(expected_dist) != len(inputs['distributions']) or expected_dist != audited_dist:
        raise ValueError('Notice input distributions do not match audited candidate')
    for row in inputs['distributions']:
        if digest(inside_file(app, row['metadata_path'])) != row['metadata_sha256']:
            raise ValueError('Candidate distribution metadata changed')
    if {(f['path'], f['sha256']) for f in inputs.get('font_assets', [])} != {(f['path'], f['sha256']) for f in audit.get('fonts', [])}:
        raise ValueError('Notice input fonts do not match audited candidate')
    for row in inputs.get('font_assets', []):
        if digest(inside_file(app, row['path'])) != row['sha256']:
            raise ValueError('Candidate font does not match notice inputs')
    record = manifest.get('recipient_notices', {})
    if record.get('input_manifest_sha256') != digest(notice_inputs):
        raise ValueError('Build did not bind these notice inputs')
    copied = {row['path']: row['sha256'] for row in record.get('files', [])}
    for row in inputs['files']:
        source = inside_file(notice_inputs.parent, row['path'])
        bundle_name = 'Contents/Resources/licenses/' + row['path']
        if digest(source) != row['sha256'] or copied.get(bundle_name) != row['sha256'] or digest(inside_file(app, bundle_name)) != row['sha256']:
            raise ValueError('Recipient notice is missing or differs from sealed app')
    if set(copied) != {'Contents/Resources/licenses/' + row['path'] for row in inputs['files']}:
        raise ValueError('Unexpected notice files in build record')
    allowed = {'ffmpeg_source_delivery_review', 'standalone_runtime_components_review', 'tokenizers_native_vendored_review'}
    unexpected = [f for f in audit.get('findings', []) if f.get('code') not in allowed]
    if unexpected:
        raise ValueError('Unresolved notice audit findings: ' + ', '.join(f.get('code', '?') for f in unexpected))
    if any(not d.get('notice_files') and not d.get('matching_supplements') for d in audit['python_distributions']):
        raise ValueError('A candidate distribution lacks notice evidence')
    notice_text = inside_file(notice_inputs.parent, inputs['files'][0]['path']).read_bytes()
    audit_notice_hashes = {row['path']: row['sha256'] for row in audit.get('notice_evidence', [])}
    supplement_root = ROOT / 'packaging/macos/third-party'
    for dist in audit['python_distributions']:
        for filename in dist.get('notice_files', []):
            file = Path(filename).resolve(strict=True)
            if not file.is_relative_to(app) or digest(file) != audit_notice_hashes.get(filename) or file.read_bytes() not in notice_text:
                raise ValueError('Bundled distribution notice is absent or changed: ' + dist['name'])
        for item in dist.get('matching_supplements', []):
            file = inside_file(supplement_root, item['file'])
            if digest(file) != item['sha256'] or file.read_bytes() not in notice_text:
                raise ValueError('Distribution supplement is absent or changed: ' + dist['name'])
    tokenizers = next((d for d in audit['python_distributions'] if d.get('name', '').lower() == 'tokenizers'), None)
    supplements = tokenizers.get('matching_supplements', []) if tokenizers else []
    rust = next((row for row in supplements if row.get('file') == 'tokenizers-0.23.3-rust-notices.txt'), None)
    if not rust:
        raise ValueError('Verified tokenizers Rust notice evidence is missing')
    rust_evidence = inside_file(ROOT / 'packaging/macos/third-party', rust['comparison_file'])
    if digest(rust_evidence) != rust['comparison_sha256']:
        raise ValueError('Tokenizers Rust evidence hash mismatch')
    crates = json.loads(rust_evidence.read_text())['packages']
    registry = [c for c in crates if c.get('status') == 'verified']
    if len(registry) != 151 or any(not c.get('notices') for c in registry) or any(c.get('status') == 'error' for c in crates):
        raise ValueError('Tokenizers source-lock notice collection is incomplete')
    standalone_dir, standalone_sources = Path(standalone_dir), Path(standalone_sources)
    standalone = json.loads((standalone_dir / 'provenance.json').read_text())
    if digest(standalone_dir / 'install-match-evidence.json') != standalone.get('install_evidence_sha256'):
        raise ValueError('Standalone release-match evidence differs from provenance')
    if standalone.get('python_version') != manifest.get('python') or (app / 'Contents/Resources/python/BUILD').read_text().strip() != standalone.get('pbs_release'):
        raise ValueError('Standalone version and build do not match')
    for name, sha in standalone['license_files'].items():
        file = inside_file(standalone_dir, 'licenses/' + name)
        if digest(file) != sha or file.read_bytes() not in notice_text:
            raise ValueError('Standalone notice is absent or changed: ' + name)
    media_info = validate_media_archive(Path(media_archive), manifest.get('ffmpeg'))
    source_names = {'cpython-3.12.12.tar.xz': standalone['pbs_source_input_pins']['cpython-3.12']['sha256'],
                    'tcl8.6.14-src.tar.gz': standalone['tcl_source']['sha256'],
                    'tk8.6.14-src.tar.gz': standalone['tk_source']['sha256'],
                    'pbs-build-source.tar.gz': standalone['pbs_source_snapshot_sha256']}
    for name, sha in source_names.items():
        if digest(inside_file(standalone_sources, name)) != sha:
            raise ValueError('Standalone source missing or changed: ' + name)
    snapshot = inside_file(app.parent, manifest['source_snapshot']['file'])
    if digest(snapshot) != manifest['source_snapshot']['sha256']:
        raise ValueError('Internal build source snapshot differs from build manifest')
    with tarfile.open(snapshot, 'r:*') as src:
        script = src.extractfile('scripts/package_macos.py').read()
    if hashlib.sha256(script).hexdigest() != manifest['packaging_script_sha256']:
        raise ValueError('Packaging script does not match actual build provenance')
    packages = ['Contents/Resources/python/lib/python3.12/site-packages/certifi',
                'Contents/Resources/python/lib/python3.12/site-packages/tqdm',
                'Contents/Resources/python/lib/python3.12/site-packages/setuptools/_vendor/autocommand']
    editable_hashes = {}
    for item in packages:
        folder = (app / item).resolve(strict=True)
        if not folder.is_relative_to(app) or not folder.is_dir() or not any(folder.rglob('*.py')):
            raise ValueError('Editable Python source missing: ' + item)
        if any(p.is_symlink() for p in folder.rglob('*')):
            raise ValueError('Python source contains a symlink')
        for file in folder.rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts and file.suffix != '.pyc':
                editable_hashes['python/' + str(file.relative_to(app))] = digest(file)
    license_materials = []
    evidence_hashes = {row['path']: row['sha256'] for row in audit.get('notice_evidence', [])}
    for package in ('certifi', 'tqdm', 'autocommand'):
        dist = next((d for d in audit['python_distributions'] if d['name'].lower() == package), None)
        if not dist or not dist.get('notice_files'):
            raise ValueError('Editable Python package license missing: ' + package)
        for filename in dist['notice_files']:
            path = Path(filename).resolve(strict=True)
            if not path.is_relative_to(app) or not path.is_file() or digest(path) != evidence_hashes.get(filename):
                raise ValueError('Editable Python license does not match audit: ' + package)
            license_materials.append((package, path))
    output.mkdir(parents=True, exist_ok=True)
    notice = output / 'THIRD_PARTY_NOTICES.txt'
    shutil.copyfile(inside_file(notice_inputs.parent, inputs['files'][0]['path']), notice)
    source_bundle = output / 'corresponding-source.tar'
    if source_bundle.exists():
        raise ValueError('Output source bundle exists; choose a fresh output directory')
    readme = ("Clippie assisted-alpha source materials\n\nThe media-input archive contains source, patches, installed Homebrew formulae and build evidence. "
              "Standalone source archives contain CPython, Tcl, Tk and python-build-standalone build scripts. "
              "Editable copies of certifi, tqdm and setuptools-vendored autocommand are included with their app-relative paths; "
              "their license files are included in python-licenses and their notices also appear in THIRD_PARTY_NOTICES.txt. package_macos.py is the actual recorded packaging script. "
              "A suitable Homebrew and Apple toolchain are needed to rebuild; this archive does not itself prove a reproducible build.\n").encode()
    with tarfile.open(source_bundle, 'x') as dst:
        dst.add(media_archive, arcname='media/ffmpeg-homebrew-source-inputs.tar', recursive=False)
        for name in source_names:
            dst.add(standalone_sources / name, arcname='standalone/' + name, recursive=False)
        for folder in packages:
            dst.add(app / folder, arcname='python/' + folder, filter=lambda info: None if '__pycache__' in Path(info.name).parts or info.name.endswith('.pyc') else info)
        for package, path in license_materials:
            dst.add(path, arcname='python-licenses/' + package + '/' + path.name, recursive=False)
        for name, value in [('README.txt', readme), ('build/package_macos.py', script)]:
            info = tarfile.TarInfo(name); info.size = len(value); info.mode = 0o644
            dst.addfile(info, io.BytesIO(value))
    with tarfile.open(source_bundle, 'r:') as written:
        for name, sha in editable_hashes.items():
            member = written.extractfile(name)
            if member is None or hashlib.file_digest(member, 'sha256').hexdigest() != sha:
                raise ValueError('Archived editable source differs from candidate: ' + name)
    evidence = {'schema_version': 1, 'app_cdhash': cdhash, 'build_manifest_sha256': digest(manifest_path),
                'notice_inputs_sha256': digest(notice_inputs), 'media': media_info,
                'standalone_source_hashes': source_names, 'editable_source_files': editable_hashes,
                'qualifications': ['Technical evidence, not legal certification or proof of recipient delivery.',
                    'Tokenizers source-lock notices conservatively cover all 151 locked registry crates; exact published wheel feature/build attestation is unavailable.',
                    'Standalone libpython UUID matches release/toolchain/bundle; exact binary transformations are not independently proved.',
                    'SciPy records the GCC runtime exception; do not treat it as unqualified GPL.',
                    'Model weights are not bundled; publisher terms apply to user-selected downloads.']}
    evidence_file = output / 'source-evidence.json'
    evidence_file.write_text(json.dumps(evidence, indent=2) + '\n')
    delivery = {'schema_version': 1, 'app_cdhash': cdhash, 'source_obligations_reviewed': True,
                'files': [{'kind': 'notice', 'path': notice.name, 'sha256': digest(notice)},
                          {'kind': 'source', 'path': source_bundle.name, 'sha256': digest(source_bundle), 'placement': 'separate'},
                          {'kind': 'source_evidence', 'path': evidence_file.name, 'sha256': digest(evidence_file)}]}
    review = {'schema_version': 1, 'app_cdhash': cdhash, 'technical_materials_review_complete': True, 'legal_certification': False,
              'redistribution_review_complete': False, 'redistribution_clearance': False, 'blockers': [],
              'delivery_requirements': [{'component': 'Bundled software and native licenses', 'files': [notice.name]},
                  {'component': 'Media, runtime and editable copyleft Python source evidence', 'files': [source_bundle.name, evidence_file.name]}],
              'qualifications': evidence['qualifications']}
    (output / 'review.json').write_text(json.dumps(review, indent=2) + '\n')
    (output / 'delivery.json').write_text(json.dumps(delivery, indent=2) + '\n')
    return review


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('app', 'audit', 'notice-inputs', 'media-archive', 'standalone-dir', 'standalone-sources', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    try:
        review = prepare(a.app, a.audit, a.notice_inputs, a.media_archive, a.standalone_dir, a.standalone_sources, a.output)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as e:
        p.error(str(e))
    print(json.dumps({'technical_materials_review_complete': review['technical_materials_review_complete'],
                      'redistribution_review_complete': review['redistribution_review_complete'], 'output': str(a.output)}))


if __name__ == '__main__':
    main()
