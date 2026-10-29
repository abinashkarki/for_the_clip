#!/usr/bin/env python3
"""Collect bounded, pinned source inputs as review evidence; not clearance."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'packaging/macos/third-party/source-inputs.json'
DEFAULT = ROOT / 'output/qa/mac-close-testers-2026-10-27/source-inputs'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def usage(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file() and not p.is_symlink())


def fetch(item, output, limit):
    final = output / item['filename']
    partial = output / (item['filename'] + '.partial')
    if urllib.parse.urlparse(item['url']).scheme != 'https':
        raise ValueError('HTTPS required')
    if final.exists() and digest(final) == item['sha256']:
        return {'status': 'verified_cached', 'sha256': item['sha256'], 'bytes': final.stat().st_size}
    if final.exists():
        raise ValueError('existing final file hash mismatch; retained for inspection')
    # Preserve any previous partial; do not append without a validated range response.
    if partial.exists():
        raise ValueError('partial exists; retained for inspection before retry')
    with urllib.request.urlopen(item['url'], timeout=45) as response:
        if urllib.parse.urlparse(response.geturl()).scheme != 'https':
            raise ValueError('redirect must use HTTPS')
        with partial.open('xb') as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                if usage(output) + len(chunk) > limit:
                    raise ValueError('total size cap reached; partial retained')
                stream.write(chunk)
    actual = digest(partial)
    if actual != item['sha256']:
        raise ValueError(f'hash mismatch: got {actual}; partial retained')
    partial.rename(final)
    return {'status': 'verified', 'sha256': actual, 'bytes': final.stat().st_size}


def copy_evidence(path, destination, output, cap, expected=None):
    if not path.is_file():
        return {'status': 'missing', 'source': str(path)}
    actual = digest(path)
    if expected and actual != expected:
        return {'status': 'hash_mismatch', 'source': str(path), 'sha256': actual, 'expected': expected}
    if destination.exists():
        if digest(destination) != actual:
            return {'status': 'destination_mismatch', 'source': str(path)}
    else:
        if usage(output) + path.stat().st_size > cap:
            return {'status': 'size_cap', 'source': str(path)}
        shutil.copyfile(path, destination)
    return {'status': 'captured', 'source': str(path), 'sha256': actual, 'file': destination.name}


def collect_x264(config, output, cap):
    if urllib.parse.urlparse(config['url']).scheme != 'https':
        raise ValueError('HTTPS required')
    final = output / 'x264-b35605ace3dd.tar'
    previous = output / 'collection.json'
    if final.exists():
        if previous.is_file():
            old = json.loads(previous.read_text()).get('x264', {})
            if old.get('revision') == config['revision'] and old.get('sha256') == digest(final):
                return {**old, 'status': 'verified_cached'}
        raise ValueError('existing x264 archive has no matching previous evidence; retained')
    partial = output / (final.name + '.partial')
    if partial.exists():
        raise ValueError('x264 partial exists; retained for inspection')
    remaining = cap - usage(output)
    if remaining <= 0:
        raise ValueError('total size cap reached')
    def limit_file():
        resource.setrlimit(resource.RLIMIT_FSIZE, (remaining, remaining))
    with tempfile.TemporaryDirectory(prefix='clippie-x264-') as temp:
        commands = [
            ['git', 'init', '--bare', temp],
            ['git', '-C', temp, 'fetch', '--depth=1', config['url'], config['revision']],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True, timeout=90, preexec_fn=limit_file)
            if result.returncode:
                raise ValueError(f"{' '.join(command[:3])} failed ({result.returncode}): {result.stderr[-1000:]}")
        actual = subprocess.run(['git', '-C', temp, 'rev-parse', 'FETCH_HEAD^{commit}'],
                                capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        if actual != config['revision']:
            raise ValueError(f'commit mismatch: {actual}')
        tree = subprocess.run(['git', '-C', temp, 'rev-parse', 'FETCH_HEAD^{tree}'],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        with partial.open('xb') as stream:
            result = subprocess.run(['git', '-C', temp, 'archive', '--format=tar', actual],
                                    stdout=stream, stderr=subprocess.PIPE, timeout=45, preexec_fn=limit_file)
        if result.returncode:
            raise ValueError(f'git archive failed ({result.returncode}): {result.stderr.decode(errors="replace")[-1000:]}')
        if usage(output) > cap:
            raise ValueError('total size cap reached; partial retained')
        checksum = digest(partial)
        partial.rename(final)
        return {'status': 'verified_git_commit', 'url': config['url'], 'revision': actual,
                'tree': tree, 'file': final.name, 'sha256': checksum, 'bytes': final.stat().st_size}


def collect(config, output, app):
    if output.resolve().is_relative_to(app.resolve()):
        raise ValueError('Output must be outside app bundle')
    output.mkdir(parents=True, exist_ok=True)
    limit = config['max_total_bytes']
    cap = limit - 65536  # Reserve space for the final manifest.
    if cap <= 0:
        raise ValueError('size cap is too small for the manifest reserve')
    results = {'schema_version': 1, 'complete_corresponding_source': False,
               'redistribution_clearance': False, 'limit_bytes': limit, 'downloads': {}, 'evidence': {}}
    for item in config['downloads']:
        try:
            results['downloads'][item['id']] = fetch(item, output, cap)
        except Exception as exc:
            errors = [f"{item['url']}: {exc}"]
            result = None
            for index, mirror in enumerate(item.get('mirrors', []), 1):
                mirror_item = {**item, 'url': mirror, 'filename': item['filename'] + f'.mirror{index}'}
                try:
                    result = fetch(mirror_item, output, cap)
                    path = output / mirror_item['filename']
                    final = output / item['filename']
                    if final.exists():
                        raise ValueError('final destination already exists')
                    path.rename(final)
                    result['verified_mirror'] = mirror
                    result['previous_errors'] = errors
                    break
                except Exception as mirror_exc:
                    errors.append(f'{mirror}: {mirror_exc}')
            results['downloads'][item['id']] = result or {'status': 'failed', 'errors': errors}
    cellar = Path('/opt/homebrew/Cellar')
    files = {
        'ffmpeg_formula': (cellar / 'ffmpeg/8.0.1_3/.brew/ffmpeg.rb', 'ffmpeg-formula.rb', config['ffmpeg_formula_sha256']),
        'ffmpeg_receipt': (cellar / 'ffmpeg/8.0.1_3/INSTALL_RECEIPT.json', 'ffmpeg-receipt.json', config['ffmpeg_receipt_sha256']),
        'ffmpeg_sbom': (cellar / 'ffmpeg/8.0.1_3/sbom.spdx.json', 'ffmpeg-sbom.json', config['ffmpeg_sbom_sha256']),
        'link_rewrite_script': (ROOT / 'scripts/package_macos.py', 'package_macos.py', None),
    }
    for package, version in [('dav1d', '1.5.3'), ('lame', '3.100'), ('libvpx', '1.15.2'), ('openssl@3', '3.6.1'), ('opus', '1.6.1'), ('svt-av1', '4.0.1'), ('x264', 'r3222'), ('x265', '4.1')]:
        root = cellar / package / version
        for label, relative in [('formula', f'.brew/{package}.rb'), ('receipt', 'INSTALL_RECEIPT.json'), ('sbom', 'sbom.spdx.json')]:
            files[f'{package}_{label}'] = (root / relative, f'{package}-{label}' + ('.rb' if label == 'formula' else '.json'), None)
    files['collector_script'] = (ROOT / 'scripts/collect_macos_source_inputs.py', 'collector-script-v3.py', None)
    files['source_pins'] = (CONFIG, 'source-pins.json', None)
    files['app_build_manifest'] = (app / 'Contents/Resources/build-manifest.json', 'app-build-manifest.json', None)
    files['patch_reconstruction'] = (ROOT / 'packaging/macos/third-party/source-evidence/reconstruct_ffmpeg_svt_patch.sh', 'reconstruct_ffmpeg_svt_patch.sh', None)
    for key, (src, name, expected) in files.items():
        results['evidence'][key] = copy_evidence(src, output / name, output, cap, expected)
    patch = results['downloads'].get('ffmpeg-svt-patch', {})
    if patch.get('status', '').startswith('verified'):
        patch['reconstruction'] = 'Pinned FFmpeg Git commit a5d4c398b411a00ac09d8fe3b66117222323844c; Git format-patch with core.abbrev=10 and signature 2.25.1; exact installed formula SHA256 verified. See reconstruct_ffmpeg_svt_patch.sh.'
    results['installed_notices'] = {}
    for package, version in [('ffmpeg', '8.0.1_3'), ('dav1d', '1.5.3'), ('lame', '3.100'), ('libvpx', '1.15.2'), ('openssl@3', '3.6.1'), ('opus', '1.6.1'), ('svt-av1', '4.0.1'), ('x264', 'r3222'), ('x265', '4.1')]:
        prefix = cellar / package / version
        candidates = [p for p in prefix.iterdir() if p.is_file() and p.name.lower().startswith(('license', 'licence', 'copying', 'notice', 'copyright'))] if prefix.is_dir() else []
        for sub in ('share/licenses', 'share/doc'):
            directory = prefix / sub
            if directory.is_dir():
                candidates.extend(p for p in directory.rglob('*') if p.is_file() and p.name.lower().startswith(('license', 'licence', 'copying', 'notice', 'copyright')))
        rows = []
        for src in sorted(set(candidates)):
            if not src.resolve().is_relative_to(prefix.resolve()):
                continue
            dst = output / 'installed-notices' / package / src.relative_to(prefix)
            dst.parent.mkdir(parents=True, exist_ok=True)
            row = copy_evidence(src, dst, output, cap)
            row['file'] = str(dst.relative_to(output))
            rows.append(row)
        results['installed_notices'][package] = rows
    binary = app / 'Contents/Resources/bin/ffmpeg'
    try:
        probe = subprocess.run([str(binary), '-buildconf'], capture_output=True, timeout=30, check=False)
        data = probe.stdout + b'\nSTDERR:\n' + probe.stderr
        if usage(output) + len(data) > cap:
            raise ValueError('size cap reached')
        (output / 'ffmpeg-buildconf.txt').write_bytes(data)
        results['evidence']['buildconf'] = {'status': 'captured' if probe.returncode == 0 else 'failed',
            'returncode': probe.returncode, 'sha256': digest(output / 'ffmpeg-buildconf.txt'), 'file': 'ffmpeg-buildconf.txt',
            'binary_sha256': digest(binary)}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        results['evidence']['buildconf'] = {'status': 'failed', 'error': str(exc)}
    try:
        results['x264'] = collect_x264(config['x264'], output, cap)
    except (Exception, subprocess.TimeoutExpired) as exc:
        results['x264'] = {'status': 'failed', 'error': str(exc), **config['x264']}
    results['bytes_used'] = usage(output)
    manifest = (json.dumps(results, indent=2) + '\n').encode()
    if len(manifest) > 65536:
        raise ValueError('manifest exceeds reserved space')
    (output / 'collection.json').write_bytes(manifest)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT)
    parser.add_argument('--app', type=Path, default=ROOT / 'output/macos-spike/Clippie.app')
    args = parser.parse_args()
    result = collect(json.loads(CONFIG.read_text()), args.output, args.app)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
