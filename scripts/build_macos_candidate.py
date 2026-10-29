#!/usr/bin/env python3
"""Rebuild the frontend and package one versioned Mac candidate with source checks."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {'node_modules', '__pycache__', '.git', '.DS_Store', 'dist'}
GENERATED_PUBLIC = {'third-party-notices.txt', 'third-party-notices.provenance.json'}


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def tree_files(root, *, exclude_generated_public=False):
    rows = []
    if not root.exists():
        raise ValueError(f'Missing source: {root}')
    for directory, subdirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        subdirs[:] = sorted(name for name in subdirs if name not in EXCLUDE)
        for name in sorted(subdirs):
            path = base / name
            if path.is_symlink():
                raise ValueError(f'Symlink in source inventory: {path}')
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(root)
            if name in EXCLUDE or name.endswith('.pyc'):
                continue
            if exclude_generated_public and relative.parts[:1] == ('public',) and name in GENERATED_PUBLIC:
                continue
            if path.is_symlink():
                raise ValueError(f'Symlink in source inventory: {path}')
            if path.is_file():
                rows.append({'path': relative.as_posix(), 'sha256': sha(path)})
    rows.sort(key=lambda row: row['path'])
    return rows


def frontend_inputs(root=ROOT):
    return tree_files(root / 'web', exclude_generated_public=True)


def assets(root=ROOT):
    return tree_files(root / 'clippie/static/studio')


def source_state(root=ROOT):
    state = {}
    for name in ('clippie', 'web', 'packaging/macos', 'scripts'):
        state[name] = tree_files(root / name, exclude_generated_public=(name == 'web'))
    for name in ('README.md', 'pyproject.toml', 'uv.lock'):
        state[name] = sha(root / name)
    return state


def prebuild_inputs(root=ROOT):
    state = source_state(root)
    state['clippie'] = [row for row in state['clippie'] if not row['path'].startswith('static/studio/')]
    return state


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify_candidate(app, expected):
    resource = app / 'Contents/Resources'
    built = tree_files(resource / 'app/clippie/static/studio')
    if built != expected['assets']:
        raise ValueError('Packaged frontend assets do not match the rebuilt assets')
    record = json.loads((resource / 'build-manifest.json').read_text())
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if record.get('frontend_provenance') != expected:
        raise ValueError('Packaged frontend provenance differs')
    if (record.get('app_version'), record.get('build_number')) != (info.get('CFBundleShortVersionString'), info.get('CFBundleVersion')):
        raise ValueError('Manifest and app version metadata differ')
    return record


def build(args, *, root=ROOT, execute=subprocess.run):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', args.app_version) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}', args.build_number):
        raise ValueError('App version must have three numeric components and build number one to three numeric components')
    work = Path(args.work_dir).expanduser().resolve()
    for source in ('clippie', 'web', 'packaging/macos', 'scripts'):
        if work.is_relative_to((root / source).resolve()):
            raise ValueError('Work directory must be outside monitored source directories')
    work.mkdir(parents=True, exist_ok=True)
    if (work / 'Clippie.app').exists():
        raise ValueError('Work directory already contains Clippie.app; choose a new directory or move the existing app')
    initial_sources = prebuild_inputs(root)
    before = frontend_inputs(root)
    execute(['npm', 'run', 'build'], cwd=root / 'web', check=True)
    after = frontend_inputs(root)
    if before != after:
        raise ValueError('Frontend inputs changed during the frontend build')
    if prebuild_inputs(root) != initial_sources:
        raise ValueError('Source changed during the frontend build')
    current_assets = assets(root)
    if not current_assets or not any(item['path'] == 'index.html' for item in current_assets):
        raise ValueError('Frontend build did not produce an index and assets')
    frozen = source_state(root)
    if frozen['web'] != after or assets(root) != current_assets:
        raise ValueError('Source changed after the frontend build')
    provenance = {'schema_version': 1, 'inputs': after, 'inputs_sha256': fingerprint(after),
                  'assets': current_assets, 'assets_sha256': fingerprint(current_assets),
                  'source_tree_sha256': fingerprint(frozen)}
    stamp = work / 'frontend-provenance.json'
    stamp.write_text(json.dumps(provenance, indent=2) + '\n')
    command = [sys.executable, str(root / 'scripts/package_macos.py'), '--work-dir', str(work),
               '--app-version', args.app_version, '--build-number', args.build_number,
               '--frontend-provenance', str(stamp), '--uv', args.uv, '--identity', args.identity]
    if args.ffmpeg: command += ['--ffmpeg', args.ffmpeg]
    if args.ffprobe: command += ['--ffprobe', args.ffprobe]
    if args.notices_dir: command += ['--notices-dir', str(args.notices_dir)]
    if args.timestamp: command.append('--timestamp')
    app = work / 'Clippie.app'
    try:
        execute(command, cwd=root, check=True)
        if source_state(root) != frozen:
            raise ValueError('Source changed during packaging; candidate is invalid')
        record = verify_candidate(app, provenance)
        if (record['app_version'], record['build_number']) != (args.app_version, args.build_number):
            raise ValueError('Candidate version differs from requested version')
    except Exception:
        if app.exists():
            quarantine = work / ('Clippie.invalid-' + uuid.uuid4().hex + '.app')
            app.rename(quarantine)
            print(f'Invalid candidate retained at {quarantine}', file=sys.stderr)
        raise
    print(json.dumps({'app': str(app), 'app_version': args.app_version, 'build_number': args.build_number,
                      'frontend_inputs_sha256': provenance['inputs_sha256'], 'frontend_assets_sha256': provenance['assets_sha256']}))
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-version', required=True)
    parser.add_argument('--build-number', required=True)
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--uv', default='uv')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    parser.add_argument('--identity', default='-')
    parser.add_argument('--timestamp', action='store_true')
    parser.add_argument('--notices-dir', type=Path)
    args = parser.parse_args()
    build(args)


if __name__ == '__main__':
    main()
