#!/usr/bin/env python3
"""Package only hash-verified media source evidence, without asserting completeness."""
import hashlib
import json
from pathlib import Path
import tarfile
import sys


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def assemble(root, destination, readme):
    data = json.loads((root / 'collection.json').read_text())
    pins = json.loads((root / 'source-pins.json').read_text())
    members = {'collection.json': digest(root / 'collection.json')}
    for item in pins['downloads']:
        result = data['downloads'][item['id']]
        if result['status'] not in ('verified', 'verified_cached'):
            raise ValueError(f"unverified input: {item['id']}")
        members[item['filename']] = item['sha256']
    x264 = data['x264']
    if x264['status'] not in ('verified_git_commit', 'verified_cached'):
        raise ValueError('unverified x264')
    members[x264['file']] = x264['sha256']
    for name, row in data['evidence'].items():
        if row['status'] == 'missing' and name == 'lame_sbom':
            continue
        if row['status'] != 'captured':
            raise ValueError(f'unverified evidence: {name}')
        members[row['file']] = row['sha256']
    for rows in data['installed_notices'].values():
        for row in rows:
            if row['status'] != 'captured':
                raise ValueError('unverified installed notice')
            members[row['file']] = row['sha256']
    for name, expected in members.items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or digest(path) != expected:
            raise ValueError(f'missing or changed input: {name}')
    readme_data = readme.read_bytes()
    sums = ''.join(f'{value}  {name}\n' for name, value in sorted(members.items()))
    sums += f'{hashlib.sha256(readme_data).hexdigest()}  README.txt\n'
    sums_bytes = sums.encode()
    import io
    if destination.exists():
        raise ValueError('destination exists')
    with tarfile.open(destination, 'x') as archive:
        for name in sorted(members):
            archive.add(root / name, arcname=name, recursive=False)
        for name, content in [('README.txt', readme_data), ('SHA256SUMS', sums_bytes)]:
            info = tarfile.TarInfo(name); info.size = len(content); info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    return digest(destination)


if __name__ == '__main__':
    print(assemble(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
