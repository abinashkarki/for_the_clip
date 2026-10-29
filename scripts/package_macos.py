#!/usr/bin/env python3
"""Build a local arm64 runtime spike; not a notarized distribution release.

Build tools only: uv 0.9.5+, Xcode CLI tools, an existing FFmpeg build. The
result contains its own Python and dependencies, never the developer's venv.
See packaging/macos/README.md for gates that this prototype does not satisfy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import plistlib
import shutil
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = '3.12.12'
EXTRAS = ('local', 'semantic', 'vision')
MACHO = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}


def run(args, **kwargs):
    try:
        return subprocess.run([str(a) for a in args], check=True, text=True, **kwargs)
    except subprocess.CalledProcessError as error:
        if error.stderr:
            print(error.stderr[-4000:], file=sys.stderr, flush=True)
        raise


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def macho(path):
    if not path.is_file() or path.is_symlink():
        return False
    with path.open('rb') as stream:
        return stream.read(4) in MACHO


def load_commands(path, *, metadata=None):
    output = run(['/usr/bin/otool', '-l', path], capture_output=True).stdout
    deps, rpaths, identity, command = [], [], None, None
    for line in output.splitlines():
        fields = line.strip().split()
        if fields[:1] == ['cmd']:
            command = fields[1]
        elif fields[:1] == ['name'] and command in {'LC_LOAD_DYLIB', 'LC_LOAD_WEAK_DYLIB', 'LC_REEXPORT_DYLIB', 'LC_ID_DYLIB'}:
            name = line.strip()[5:].rsplit(' (offset ', 1)[0]
            if command == 'LC_ID_DYLIB':
                identity = name
            else:
                deps.append(name)
        elif fields[:1] == ['path'] and command == 'LC_RPATH':
            rpaths.append(line.strip()[5:].rsplit(' (offset ', 1)[0])
        elif metadata is not None and (fields[:1] == ['minos'] or (command == 'LC_VERSION_MIN_MACOSX' and fields[:1] == ['version'])):
            metadata.setdefault('minimum_macos', []).append(fields[1])
    return deps, rpaths, identity


def linked(path):
    return load_commands(path)[0]


def system_library(path):
    return path.startswith(('/usr/lib/', '/System/Library/'))


def bundle_ffmpeg(executables, resources):
    """Copy the complete local dylib closure, rewrite all non-system references.

    This records local binary provenance; it does not resolve distribution terms.
    Fail closed on rpath/framework inputs that need a more specialized recipe.
    """
    binaries, libraries = resources / 'bin', resources / 'lib'
    binaries.mkdir()
    libraries.mkdir()
    copied, targets, sources = {}, {}, {}

    def copy(source, target):
        source = Path(source).resolve(strict=True)
        if source in copied:
            return copied[source]
        if target in targets and targets[target] != source:
            raise RuntimeError(f'Dylib basename collision: {target.name}')
        targets[target] = source
        copied[source] = target
        shutil.copy2(source, target)
        target.chmod(target.stat().st_mode | stat.S_IWUSR)
        sources[str(target.relative_to(resources))] = {'source': str(source), 'source_sha256': digest(source)}
        # Copy recursively before rewriting so all source load commands resolve.
        for reference in linked(source):
            if system_library(reference):
                continue
            if reference.startswith('@loader_path/'):
                dependency = source.parent / reference[len('@loader_path/'):]
            elif reference.startswith('/'):
                dependency = Path(reference)
            else:
                raise RuntimeError(f'Unsupported FFmpeg load command {reference} in {source}; provide a fully resolved build.')
            if dependency.resolve() == source:
                continue  # dylib install ID
            destination = copy(dependency, libraries / dependency.resolve().name)
            replacement = '@loader_path/' + os.path.relpath(destination, target.parent)
            run(['/usr/bin/install_name_tool', '-change', reference, replacement, target], capture_output=True)
        if target.suffix == '.dylib':
            run(['/usr/bin/install_name_tool', '-id', '@loader_path/' + target.name, target], capture_output=True)
        return target

    for name, source in executables.items():
        copy(source, binaries / name)
    return sources


def prepare_runtime(work, uv):
    toolchains = work / 'toolchains'
    run([uv, 'python', 'install', PYTHON_VERSION, '--install-dir', toolchains, '--no-bin'])
    matches = list(toolchains.glob(f'cpython-{PYTHON_VERSION}-macos-aarch64-none'))
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one downloaded standalone arm64 runtime.')
    source = matches[0]
    runtime = work / 'runtime'
    requirements = work / 'requirements.txt'
    command = [uv, 'export', '--locked', '--no-dev', '--no-emit-project', '--output-file', requirements]
    for extra in EXTRAS:
        command.extend(['--extra', extra])
    # Check the project/lock contract even on a runtime cache hit. A newly added
    # dependency must not disappear silently behind --frozen or a stale cache.
    run(command, cwd=ROOT, stdout=subprocess.DEVNULL)
    fingerprint = hashlib.sha256((digest(ROOT / 'uv.lock') + digest(ROOT / 'pyproject.toml') + PYTHON_VERSION + ','.join(EXTRAS)).encode()).hexdigest()
    stamp = work / 'runtime-fingerprint.txt'
    if runtime.exists() and (work / 'requirements.txt').is_file() and stamp.is_file() and stamp.read_text() == fingerprint:
        return runtime
    if runtime.exists():
        shutil.rmtree(runtime)
    shutil.copytree(source, runtime, symlinks=True)
    # --python names only the new private interpreter; never touches system Python.
    run([uv, 'pip', 'sync', '--python', runtime / 'bin/python3', '--system', '--break-system-packages',
         '--require-hashes', '--only-binary', ':all:', '--link-mode', 'copy', requirements])
    stamp.write_text(fingerprint)
    return runtime


def audit(app):
    """Resolve native dependencies and reject unresolved/out-of-bundle loads."""
    records, errors = [], []
    executable = app / 'Contents/Resources/python/bin/python3'

    def expand(reference, loader):
        return Path(reference.replace('@loader_path', str(loader.parent)).replace('@executable_path', str(executable.parent))).resolve()

    _, main_rpaths, _ = load_commands(executable)
    for path in sorted(app.rglob('*')):
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(app.resolve()):
                raise RuntimeError(f'External symlink in bundle: {path}')
        if not macho(path):
            continue
        metadata = {}
        deps, rpaths, identity = load_commands(path, metadata=metadata)
        search = [expand(ref, path) for ref in rpaths] + [expand(ref, executable) for ref in main_rpaths]
        for directory in search:
            if not directory.is_relative_to(app.resolve()) and not system_library(str(directory) + '/'):
                errors.append(f'External RPATH in {path.relative_to(app)}: {directory}')
        for dep in deps:
            if system_library(dep):
                continue
            if dep.startswith('@rpath/'):
                candidates = [directory / dep[len('@rpath/'):] for directory in search]
                resolved = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
            else:
                resolved = expand(dep, path)
            if resolved is None or not resolved.is_relative_to(app.resolve()) or not resolved.is_file():
                errors.append(f'Unresolved/external dependency in {path.relative_to(app)}: {dep}')
        arch = run(['/usr/bin/lipo', '-archs', path], capture_output=True).stdout.strip()
        if 'arm64' not in arch.split():
            raise RuntimeError(f'No arm64 slice in {path}: {arch}')
        records.append({'path': str(path.relative_to(app)), 'architectures': arch, 'dependencies': deps, 'rpaths': rpaths, **metadata})
    if errors:
        raise RuntimeError('\n'.join(errors))
    return records


def sanitize_native_rpaths(app):
    """Wheel build-host search paths must never be consulted on a user's Mac."""
    removed = []
    executable = app / 'Contents/Resources/python/bin/python3'
    for path in sorted(app.rglob('*')):
        if not macho(path):
            continue
        deps, rpaths, _ = load_commands(path)
        if 'torchvision' in path.relative_to(app).parts:
            torch_lib = app / 'Contents/Resources/python/lib/python3.12/site-packages/torch/lib'
            for reference in list(deps):
                if reference.startswith('@rpath/') and (torch_lib / reference[len('@rpath/'):]).is_file():
                    replacement = '@loader_path/' + os.path.relpath(torch_lib / reference[len('@rpath/'):], path.parent)
                    run(['/usr/bin/install_name_tool', '-change', reference, replacement, path], capture_output=True)
                    removed.append({'path': str(path.relative_to(app)), 'dependency': reference, 'replacement': replacement})
                    deps.remove(reference)
        # The macOS Numba wheel leaves its OpenMP backend unresolved. Torch in
        # this locked runtime supplies LLVM libomp; share that one runtime rather
        # than depend on Homebrew or load two competing copies. The fixture probe
        # exercises both Torch and Numba before accepting this compatibility edge.
        if path.name.startswith('omppool.') and path.parent.parts[-3:] == ('numba', 'np', 'ufunc'):
            omp = app / 'Contents/Resources/python/lib/python3.12/site-packages/torch/lib/libomp.dylib'
            if '@rpath/libomp.dylib' in deps:
                if not omp.is_file():
                    raise RuntimeError('Locked runtime lacks LLVM OpenMP for Numba.')
                replacement = '@loader_path/' + os.path.relpath(omp, path.parent)
                run(['/usr/bin/install_name_tool', '-change', '@rpath/libomp.dylib', replacement, path], capture_output=True)
                removed.append({'path': str(path.relative_to(app)), 'dependency': '@rpath/libomp.dylib', 'replacement': replacement})
                deps.remove('@rpath/libomp.dylib')
        # Some wheels rely on an importing extension's inherited RPATH for
        # sibling libraries (MLX's libjaccl, for example). Make that edge explicit
        # so relocation doesn't depend on which module was imported first.
        for reference in deps:
            if reference.startswith('@rpath/') and (path.parent / reference[len('@rpath/'):]).is_file():
                replacement = '@loader_path/' + reference[len('@rpath/'):]
                run(['/usr/bin/install_name_tool', '-change', reference, replacement, path], capture_output=True)
                removed.append({'path': str(path.relative_to(app)), 'dependency': reference, 'replacement': replacement})
        for reference in rpaths:
            expanded = Path(reference.replace('@loader_path', str(path.parent)).replace('@executable_path', str(executable.parent))).resolve()
            if not expanded.is_relative_to(app.resolve()) and not system_library(str(expanded) + '/'):
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
                run(['/usr/bin/install_name_tool', '-delete_rpath', reference, path], capture_output=True)
                removed.append({'path': str(path.relative_to(app)), 'rpath': reference})
    return removed


def copy_notice_inputs(app, directory):
    """Bind the recipient texts to the exact runtime metadata/font inputs."""
    directory = Path(directory).resolve(strict=True)
    manifest_path = directory / 'notice-inputs.json'
    inputs = json.loads(manifest_path.read_text())
    if inputs.get('schema_version') != 1 or not inputs.get('files') or not inputs.get('distributions'):
        raise ValueError('Notice input manifest is incomplete.')
    for item in inputs['distributions'] + inputs.get('font_assets', []):
        relative = item.get('metadata_path', item.get('path'))
        if not isinstance(relative, str):
            raise ValueError('Notice input path is invalid.')
        target = (app / relative).resolve(strict=True)
        expected = item.get('metadata_sha256', item.get('sha256'))
        if not target.is_relative_to(app.resolve()) or not target.is_file() or digest(target) != expected:
            raise ValueError('Notice input does not match the candidate runtime: ' + relative)
    output = app / 'Contents/Resources/licenses'
    output.mkdir()
    records, seen = [], set()
    for item in inputs['files']:
        relative = item['path']
        path = Path(relative)
        if (path.is_absolute() or relative != path.as_posix() or '..' in path.parts
                or relative in seen or relative == 'notice-inputs.json'):
            raise ValueError('Notice files must have unique ordinary relative paths.')
        seen.add(relative)
        source = (directory / path).resolve(strict=True)
        if not source.is_relative_to(directory) or not source.is_file() or digest(source) != item['sha256']:
            raise ValueError('Notice file is missing or changed: ' + relative)
        destination = output / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        records.append({'path': str(destination.relative_to(app)), 'sha256': digest(destination)})
    shutil.copyfile(manifest_path, output / manifest_path.name)
    return {'input_manifest_sha256': digest(manifest_path), 'files': records}


def source_snapshot(work):
    destination = work / 'build-source-snapshot.tar.gz'
    paths = [ROOT / name for name in ('clippie', 'packaging/macos', 'scripts', 'pyproject.toml', 'uv.lock',
                                      'web/package.json', 'web/package-lock.json', 'web/index.html', 'web/tsconfig.json', 'web/vite.config.ts', 'web/src', 'web/scripts', 'web/public')]
    def include(info):
        return None if any(part in {'__pycache__', '.DS_Store'} or part.endswith('.pyc') for part in Path(info.name).parts) else info
    with tarfile.open(destination, 'w:gz') as archive:
        for path in paths:
            archive.add(path, arcname=str(path.relative_to(ROOT)), filter=include)
    return {'file': destination.name, 'sha256': digest(destination), 'scope': 'internal source snapshot; not recipient source delivery'}


def set_release_version(info, app_version, build_number):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', app_version):
        raise ValueError('App version must have three numeric components.')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}', build_number):
        raise ValueError('Build number must have one to three numeric components.')
    info['CFBundleShortVersionString'] = app_version
    info['CFBundleVersion'] = build_number


def build(args):
    set_release_version({}, args.app_version, args.build_number)
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise SystemExit('Build this prototype on an Apple silicon Mac.')
    uv = Path(args.uv).expanduser().resolve() if '/' in args.uv else Path(shutil.which(args.uv) or args.uv)
    work = Path(args.work_dir).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    print('Preparing the pinned standalone runtime', flush=True)
    runtime = prepare_runtime(work, uv)
    app = work / 'Clippie.app'
    if app.exists():
        shutil.rmtree(app)
    contents = app / 'Contents'
    resources = contents / 'Resources'
    macos = contents / 'MacOS'
    resources.mkdir(parents=True)
    macos.mkdir()
    print('Copying the private runtime and application resources', flush=True)
    # APFS copy-on-write gives a fresh tree without aliasing mutable cache files.
    run(['/bin/cp', '-cR', runtime, resources / 'python'])
    # Console entry-point shebangs refer to the staging interpreter. The app uses
    # python -m exclusively; don't ship those build-only executable scripts.
    for path in (resources / 'python/bin').iterdir():
        if not path.is_symlink() and not macho(path):
            path.unlink()
    for path in (resources / 'python/bin').iterdir():
        if path.is_symlink() and not path.exists():
            path.unlink()
    # uv embeds the installation's absolute ID in this standalone dylib. Its
    # executable already loads it relatively; keep the ID relocatable too.
    libpython = resources / f'python/lib/libpython{PYTHON_VERSION.rsplit(".", 1)[0]}.dylib'
    run(['/usr/bin/install_name_tool', '-id', '@rpath/' + libpython.name, libpython], capture_output=True)
    app_code = resources / 'app'
    app_code.mkdir()
    shutil.copytree(ROOT / 'clippie', app_code / 'clippie', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    shutil.copy2(ROOT / 'README.md', app_code / 'README.md')
    print('Bundling FFmpeg and its native dependencies', flush=True)
    provenance = bundle_ffmpeg({'ffmpeg': args.ffmpeg, 'ffprobe': args.ffprobe}, resources)
    package = ROOT / 'packaging/macos'
    shutil.copy2(package / 'Info.plist', contents / 'Info.plist')
    print('Compiling the native launcher', flush=True)
    run(['/usr/bin/xcrun', 'swiftc', '-O', '-target', 'arm64-apple-macos14.0', '-framework', 'AppKit', package / 'Launcher.swift', '-o', macos / 'Clippie'])
    record = {
        'purpose': 'local runtime feasibility spike; not notarized or approved for redistribution',
        'revision': run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True).stdout.strip(),
        'dirty_checkout': bool(run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True).stdout.strip()),
        'python': PYTHON_VERSION, 'extras': EXTRAS,
        'app_version': args.app_version, 'build_number': args.build_number,
        'uv': run([uv, '--version'], capture_output=True).stdout.strip(),
        'lock_sha256': digest(ROOT / 'uv.lock'), 'requirements_sha256': digest(work / 'requirements.txt'),
        'ffmpeg': provenance, 'signing_identity': args.identity,
        'notarized': False, 'redistribution_review_complete': False,
        'packaging_script_sha256': digest(Path(__file__)),
        'source_snapshot': source_snapshot(work),
        'hardened_runtime': args.identity != '-',
        'python_entitlements': plistlib.loads((package / 'python-entitlements.plist').read_bytes()) if args.identity != '-' else {},
    }
    if args.frontend_provenance:
        record['frontend_provenance'] = json.loads(Path(args.frontend_provenance).read_text())
        for item in record['frontend_provenance']['assets']:
            path = app_code / 'clippie/static/studio' / item['path']
            if not path.resolve().is_relative_to((app_code / 'clippie/static/studio').resolve()) or not path.is_file() or digest(path) != item['sha256']:
                raise ValueError('Bundled frontend does not match rebuilt asset: ' + item['path'])
    if args.notices_dir:
        record['recipient_notices'] = copy_notice_inputs(app, args.notices_dir)
    # All native code, including dylibs/extensions, is signed inside-out.
    print('Normalizing and auditing native library paths', flush=True)
    record['removed_build_host_rpaths'] = sanitize_native_rpaths(app)
    record['native_inventory'] = audit(app)
    native_floors = [version for entry in record['native_inventory'] for version in entry.get('minimum_macos', [])]
    record['native_minimum_macos'] = max(native_floors, key=lambda value: tuple(int(part) for part in value.split('.')))
    info_path = contents / 'Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    set_release_version(info, args.app_version, args.build_number)
    info['LSMinimumSystemVersion'] = record['native_minimum_macos']
    info_path.write_bytes(plistlib.dumps(info))
    (resources / 'build-manifest.json').write_text(json.dumps(record, indent=2) + '\n')
    # Ad-hoc code has no Team ID, so hardened library validation rejects its own
    # dylibs. Keep the default local-only build unhardened; real identities enable
    # hardened runtime and must pass the same dynamic-import/inference checks.
    sign = ['/usr/bin/codesign', '--force', '--sign', args.identity]
    if args.identity != '-':
        sign.extend(['--options', 'runtime'])
    if args.timestamp:
        sign.append('--timestamp')
    print(f'Signing {len(record["native_inventory"])} native files and the app', flush=True)
    for entry in sorted(record['native_inventory'], key=lambda x: len(Path(x['path']).parts), reverse=True):
        extra = ['--entitlements', package / 'python-entitlements.plist'] if entry['path'] == 'Contents/Resources/python/bin/python3.12' and args.identity != '-' else []
        run([*sign, *extra, app / entry['path']], capture_output=True)
    run([*sign, app], capture_output=True)
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', app])
    print(json.dumps({'app': str(app), 'native_files': len(record['native_inventory']), 'notarized': False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uv', default='uv', help='uv >=0.9.5 executable (build-time only)')
    parser.add_argument('--work-dir', default=str(ROOT / 'output/macos-spike'))
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    parser.add_argument('--ffprobe', default=shutil.which('ffprobe'))
    parser.add_argument('--identity', default='-', help='codesign identity; default ad-hoc, local testing only')
    parser.add_argument('--timestamp', action='store_true', help='Request secure signing timestamps (required for a distribution release)')
    parser.add_argument('--app-version', default='0.1.0')
    parser.add_argument('--build-number', default='1')
    parser.add_argument('--frontend-provenance', type=Path, help='Frontend source and built-asset fingerprint from candidate wrapper.')
    parser.add_argument('--notices-dir', type=Path, help='Verified recipient notice files and notice-inputs.json to include before signing.')
    args = parser.parse_args()
    try:
        set_release_version({}, args.app_version, args.build_number)
    except ValueError as error:
        parser.error(str(error))
    if not args.ffmpeg or not args.ffprobe:
        parser.error('Provide FFmpeg and FFprobe build inputs.')
    build(args)


if __name__ == '__main__':
    main()
