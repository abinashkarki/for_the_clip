"""Evidence reconciliation rejects stale/tampered inputs before packaging."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prepare_macos_close_tester_review as review


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name); info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return output.getvalue()


def make_fixture(tmp_path, monkeypatch):
    root = tmp_path / 'root'
    third = root / 'packaging/macos/third-party'
    third.mkdir(parents=True)
    monkeypatch.setattr(review, 'ROOT', root)
    (third / 'tokenizers-0.23.3-rust-notices.txt').write_bytes(b'fixture license')
    rust = third / 'rust.json'
    rust.write_text(json.dumps({'packages': [{'status': 'verified', 'notices': {'LICENSE': {}}} for _ in range(151)]}))
    pins = json.dumps({'downloads': [{'filename': 'source.tar', 'sha256': sha(b'source')}]}).encode()
    (third / 'source-inputs.json').write_bytes(pins)
    ffmpeg = {'bin/ffmpeg': {'source': 'ffmpeg', 'source_sha256': 'abc'}}
    media_entries = {'source.tar': b'source', 'x264.tar': b'x264', 'source-pins.json': pins,
                     'app-build-manifest.json': json.dumps({'ffmpeg': ffmpeg}).encode(),
                     'collection.json': json.dumps({'x264': {'file': 'x264.tar', 'sha256': sha(b'x264'), 'status': 'verified_git_commit'}}).encode()}
    media_entries['SHA256SUMS'] = ''.join(f'{sha(v)}  {k}\n' for k, v in media_entries.items()).encode()
    media = tmp_path / 'media.tar'; media.write_bytes(tar_bytes(media_entries))
    app = tmp_path / 'Clippie.app'; resources = app / 'Contents/Resources'; resources.mkdir(parents=True)
    (resources / 'python').mkdir(); (resources / 'python/BUILD').write_text('build')
    metadata_rel = 'Contents/Resources/python/lib/python3.12/site-packages/tokenizers.dist-info/METADATA'
    metadata = app / metadata_rel; metadata.parent.mkdir(parents=True); metadata.write_text('fixture')
    for path in ('certifi', 'tqdm', 'setuptools/_vendor/autocommand'):
        dest = resources / 'python/lib/python3.12/site-packages' / path
        dest.mkdir(parents=True); (dest / '__init__.py').write_text('source')
    inputs = {'schema_version': 1, 'files': [{'path': 'THIRD_PARTY_NOTICES.txt', 'sha256': sha(b'fixture license')}],
              'distributions': [{'metadata_path': metadata_rel, 'metadata_sha256': review.digest(metadata)}], 'font_assets': []}
    inputs_path = tmp_path / 'notice-inputs.json'; inputs_path.write_text(json.dumps(inputs))
    (tmp_path / 'THIRD_PARTY_NOTICES.txt').write_bytes(b'fixture license')
    notices = resources / 'licenses'; notices.mkdir(); (notices / 'THIRD_PARTY_NOTICES.txt').write_bytes(b'fixture license')
    script = b'packaging script'; snapshot = tmp_path / 'snapshot.tar'; snapshot.write_bytes(tar_bytes({'scripts/package_macos.py': script}))
    manifest = {'ffmpeg': ffmpeg, 'python': '3.12.12', 'source_snapshot': {'file': snapshot.name, 'sha256': review.digest(snapshot)},
                'packaging_script_sha256': sha(script), 'recipient_notices': {'input_manifest_sha256': review.digest(inputs_path),
                'files': [{'path': 'Contents/Resources/licenses/THIRD_PARTY_NOTICES.txt', 'sha256': sha(b'fixture license')}]}}
    manifest_path = resources / 'build-manifest.json'; manifest_path.write_text(json.dumps(manifest))
    audit = {'app_cdhash': 'a' * 40, 'build_manifest': {'sha256': review.digest(manifest_path)}, 'findings': [], 'fonts': [],
             'python_distributions': [{'name': 'tokenizers', 'metadata_path': metadata_rel, 'metadata_sha256': review.digest(metadata),
                 'notice_files': [], 'matching_supplements': [{'file': 'tokenizers-0.23.3-rust-notices.txt', 'sha256': sha(b'fixture license'),
                    'comparison_file': 'rust.json', 'comparison_sha256': review.digest(rust)}]}]}
    audit['notice_evidence'] = []
    for package in ('certifi', 'tqdm', 'autocommand'):
        meta_path = f'Contents/Resources/python/lib/python3.12/site-packages/{package}.dist-info/METADATA'
        meta = app / meta_path; meta.parent.mkdir(parents=True); meta.write_text(package)
        license_path = meta.parent / 'LICENSE'; license_path.write_text('fixture license')
        audit['python_distributions'].append({'name': package, 'metadata_path': meta_path, 'metadata_sha256': review.digest(meta),
            'notice_files': [str(license_path)], 'matching_supplements': []})
        audit['notice_evidence'].append({'path': str(license_path), 'sha256': review.digest(license_path)})
        inputs['distributions'].append({'metadata_path': meta_path, 'metadata_sha256': review.digest(meta)})
    inputs_path.write_text(json.dumps(inputs))
    manifest['recipient_notices']['input_manifest_sha256'] = review.digest(inputs_path)
    manifest_path.write_text(json.dumps(manifest))
    audit['build_manifest']['sha256'] = review.digest(manifest_path)
    audit_path = tmp_path / 'audit.json'; audit_path.write_text(json.dumps(audit))
    standalone = tmp_path / 'standalone'; (standalone / 'licenses').mkdir(parents=True)
    (standalone / 'licenses/LICENSE').write_bytes(b'fixture license')
    (standalone / 'install-match-evidence.json').write_text('evidence')
    src = tmp_path / 'src'; src.mkdir()
    names = ('cpython-3.12.12.tar.xz', 'tcl8.6.14-src.tar.gz', 'tk8.6.14-src.tar.gz', 'pbs-build-source.tar.gz')
    for name in names: (src / name).write_bytes(name.encode())
    provenance = {'python_version': '3.12.12', 'pbs_release': 'build', 'license_files': {'LICENSE': sha(b'fixture license')},
                  'install_evidence_sha256': review.digest(standalone / 'install-match-evidence.json'),
                  'pbs_source_input_pins': {'cpython-3.12': {'sha256': review.digest(src / names[0])}},
                  'tcl_source': {'sha256': review.digest(src / names[1])}, 'tk_source': {'sha256': review.digest(src / names[2])},
                  'pbs_source_snapshot_sha256': review.digest(src / names[3])}
    (standalone / 'provenance.json').write_text(json.dumps(provenance))
    return app, audit_path, inputs_path, media, standalone, src


def signed(_):
    return {'passed': True, 'detail': 'CDHash=' + 'a' * 40}


def test_review_binds_materials_and_makes_recipient_sources(tmp_path, monkeypatch):
    args = make_fixture(tmp_path, monkeypatch)
    result = review.prepare(*args, tmp_path / 'delivery', run=signed)
    assert result['technical_materials_review_complete'] and not result['legal_certification']
    delivery = json.loads((tmp_path / 'delivery/delivery.json').read_text())
    assert {row['kind'] for row in delivery['files']} == {'notice', 'source', 'source_evidence'}
    with tarfile.open(tmp_path / 'delivery/corresponding-source.tar') as archive:
        names = archive.getnames()
        assert 'build/package_macos.py' in names
        assert any('autocommand/__init__.py' in n for n in names)
        assert 'media/ffmpeg-homebrew-source-inputs.tar' in names
        assert 'python-licenses/autocommand/LICENSE' in names


def test_review_rejects_changed_metadata_and_changed_media(tmp_path, monkeypatch):
    args = make_fixture(tmp_path, monkeypatch)
    app, _, _, media, _, _ = args
    (app / 'Contents/Resources/python/lib/python3.12/site-packages/tokenizers.dist-info/METADATA').write_text('changed')
    with pytest.raises(ValueError, match='metadata changed'):
        review.prepare(*args, tmp_path / 'bad1', run=signed)
    metadata = app / 'Contents/Resources/python/lib/python3.12/site-packages/tokenizers.dist-info/METADATA'
    metadata.write_text('fixture')
    media.write_bytes(tar_bytes({'wrong': b'x'}))
    with pytest.raises((ValueError, KeyError)):
        review.prepare(*args, tmp_path / 'bad2', run=signed)
