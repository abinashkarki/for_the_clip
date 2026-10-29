#!/usr/bin/env python3
"""Collect checksum-verified crate notices from the exact tokenizers sdist lock.

The source lock is evidence of potential dependencies, not proof that a published
wheel used that lock or that its vendored dependencies are fully covered.
"""
from __future__ import annotations
import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tomllib
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'packaging/macos/third-party/tokenizers-0.23.3-Cargo.lock'
LOCK_SHA = 'afcedf7f2ebe008b068d7afd25e77f7d9d8e433c24153bc35a1eb5611083ee5b'
OUTPUT = ROOT / 'output/qa/mac-close-testers-2026-10-27/tokenizers-crates'


def fetch(pkg, cache):
    name, version = pkg['name'], pkg['version']
    if pkg.get('source') != 'registry+https://github.com/rust-lang/crates.io-index' or not pkg.get('checksum'):
        return {'name': name, 'version': version, 'status': 'non_registry_or_local', 'source': pkg.get('source')}
    path = cache / f'{name}-{version}.crate'
    url = f'https://static.crates.io/crates/{name}/{name}-{version}.crate'
    try:
        raw = path.read_bytes() if path.is_file() else None
        if raw is None or hashlib.sha256(raw).hexdigest() != pkg['checksum']:
            request = Request(url, headers={'User-Agent': 'Clippie-notice-review/1.0'})
            with urlopen(request, timeout=30) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError('crate exceeds 16 MiB cap')
            if hashlib.sha256(raw).hexdigest() != pkg['checksum']:
                raise ValueError('crate SHA256 mismatch')
            path.write_bytes(raw)
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            files = {}
            cargo = None
            for member in archive:
                if not member.isfile() or member.size > 1024 * 1024:
                    continue
                relative = member.name.partition('/')[2]
                base = Path(relative).name.lower()
                if relative == 'Cargo.toml':
                    cargo = tomllib.loads(archive.extractfile(member).read().decode('utf8'))
                elif base.startswith(('license', 'licence', 'copying', 'notice', 'copyright', 'authors')):
                    contents = archive.extractfile(member).read()
                    files[relative] = {'sha256': hashlib.sha256(contents).hexdigest(),
                                       'text': contents.decode('utf8', errors='replace')}
        return {'name': name, 'version': version, 'status': 'verified', 'url': url,
                'sha256': pkg['checksum'], 'declared_license': (cargo or {}).get('package', {}).get('license'),
                'license_file': (cargo or {}).get('package', {}).get('license-file'), 'notices': files}
    except (OSError, ValueError, URLError, tarfile.TarError, tomllib.TOMLDecodeError) as exc:
        return {'name': name, 'version': version, 'status': 'error', 'detail': str(exc)}


def supplement_defmt(item, cache):
    """Associate upstream root licenses only after all packaged src files match."""
    if item.get('name') != 'defmt-parser' or item.get('version') != '1.0.0' or item.get('notices'):
        return
    revision = '4a8cdb44891ed57b8ff5a023b6bec7137c48708f'
    url = f'https://codeload.github.com/knurling-rs/defmt/tar.gz/{revision}'
    try:
        with urlopen(Request(url, headers={'User-Agent': 'Clippie-notice-review/1.0'}), timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('upstream archive exceeds cap')
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as upstream, tarfile.open(cache / 'defmt-parser-1.0.0.crate') as crate:
            members = {m.name.partition('/')[2]: m for m in upstream if m.isfile() and '/' in m.name}
            local = [m for m in crate if m.isfile() and m.name.partition('/')[2].startswith('src/')]
            if not local or any('parser/' + m.name.partition('/')[2] not in members or
                    crate.extractfile(m).read() != upstream.extractfile(members['parser/' + m.name.partition('/')[2]]).read() for m in local):
                raise ValueError('crate source files differ from upstream pinned revision')
            for name in ('LICENSE-APACHE', 'LICENSE-MIT'):
                contents = upstream.extractfile(members[name]).read()
                item['notices']['upstream-root/' + name] = {'sha256': hashlib.sha256(contents).hexdigest(),
                                                          'text': contents.decode('utf8', errors='replace')}
        item['supplement'] = {'url': url, 'revision': revision, 'archive_sha256': hashlib.sha256(raw).hexdigest(),
                              'source_files_matched': len(local), 'qualification': 'Pinned upstream root license; all packaged src files matched.'}
    except (OSError, ValueError, URLError, tarfile.TarError, KeyError) as exc:
        item['supplement_error'] = str(exc)


def main():
    raw = LOCK.read_bytes()
    if hashlib.sha256(raw).hexdigest() != LOCK_SHA:
        raise ValueError('Tokenizers sdist lock hash changed.')
    packages = tomllib.loads(raw.decode())['package']
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cache = OUTPUT / 'crates'
    cache.mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda pkg: fetch(pkg, cache), packages))
    for item in results:
        supplement_defmt(item, cache)
    result = {'schema_version': 1, 'lock_sha256': LOCK_SHA,
              'limitation': 'Cargo.lock is source-package evidence; wheel build features and exact lock use have not been independently established.',
              'packages': results}
    (OUTPUT / 'crate-notice-evidence.json').write_text(json.dumps(result, indent=2) + '\n')
    lines = ['Tokenizers 0.23.3 source-lock crate notice evidence; this is not proof of the published wheel build.']
    for item in results:
        for name, notice in item.get('notices', {}).items():
            lines.append(f"\n=== {item['name']} {item['version']} {name} SHA256 {notice['sha256']} ===\n{notice['text']}")
    (OUTPUT / 'RUST_CRATE_NOTICES.txt').write_text('\n'.join(lines))
    print(json.dumps({'packages': len(results), 'verified': sum(x['status'] == 'verified' for x in results),
                      'errors': sum(x['status'] == 'error' for x in results),
                      'without_notice_text': sum(x['status'] == 'verified' and not x['notices'] for x in results)}))


if __name__ == '__main__':
    main()
