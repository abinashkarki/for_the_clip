"""Review evidence remains unresolved despite successful local collection."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('audit_macos_notices', Path(__file__).resolve().parents[1] / 'scripts/audit_macos_notices.py')
notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notices)


def fixture_app(tmp_path):
    app = tmp_path / 'Fixture.app'
    resources = app / 'Contents/Resources'
    site = resources / 'python/lib/python3.12/site-packages'
    dist = site / 'example-1.dist-info'
    dist.mkdir(parents=True)
    (dist / 'METADATA').write_text('Name: example\nVersion: 1\nLicense-Expression: MIT\nLicense-File: LICENSE\nLicense-File: MISSING.txt\nProject-URL: Source, https://example.test/source\n')
    (dist / 'licenses').mkdir()
    (dist / 'licenses/LICENSE').write_text('Example permission notice\n')
    (site / 'example').mkdir()
    native = site / 'example/native.so'
    native.write_bytes(b'fixture-native')
    (dist / 'RECORD').write_text('example/native.so,,\n')
    (resources / 'python/LICENSE.txt').write_text('Standalone Python notice\n')
    (resources / 'bin').mkdir()
    (resources / 'bin/ffmpeg').write_text('fixture')
    (resources / 'bin/ffprobe').write_text('fixture')
    font = resources / 'app/fonts/Hanken.woff2'
    font.parent.mkdir(parents=True)
    font.write_bytes(b'font-fixture')
    manifest = {'python': '3.12.12', 'ffmpeg': {}, 'native_inventory': [
        {'path': str(native.relative_to(app)), 'dependencies': []}]}
    (resources / 'build-manifest.json').write_text(json.dumps(manifest))
    return app


def test_collects_notices_hashes_and_unresolved_review(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    monkeypatch.setattr(notices, 'capture', lambda *args: {'returncode': 0, 'stdout': 'GPL build fixture', 'stderr': ''})
    before = {str(p): p.read_bytes() for p in app.rglob('*') if p.is_file()}
    result = notices.audit(app, tmp_path / 'review')
    assert result['schema_version'] == 1
    assert result['app_cdhash'] is None
    assert result['redistribution_review_complete'] is False
    assert result['redistribution_clearance'] is False
    assert result['blockers'] == result['findings']
    dist = result['python_distributions'][0]
    assert dist['name'] == 'example' and dist['version'] == '1'
    assert dist['missing_declared_license_files'] == ['MISSING.txt']
    assert result['native_components'][0]['wheel_owners'] == ['example']
    assert result['native_components'][0]['bundled_sha256']
    assert result['native_components'][0]['license_mapping_status'] == 'wheel distribution notice association; vendored coverage not implied'
    codes = {finding['code'] for finding in result['findings']}
    assert {'font_notice_unmapped', 'ffmpeg_source_delivery_review', 'declared_notice_missing'} <= codes
    assembled = (tmp_path / 'review/THIRD_PARTY_NOTICES.txt').read_text()
    assert 'Example permission notice' in assembled and 'Standalone Python notice' in assembled
    assert before == {str(p): p.read_bytes() for p in app.rglob('*') if p.is_file()}


def test_rejects_output_in_bundle_before_writing(tmp_path):
    app = fixture_app(tmp_path)
    with pytest.raises(ValueError, match='outside'):
        notices.audit(app, app / 'review')
    assert not (app / 'review').exists()


def test_external_notice_symlink_not_ingested(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    external = tmp_path / 'secret-license'
    external.write_text('must not be ingested')
    (app / 'Contents/Resources/LICENSE-external').symlink_to(external)
    monkeypatch.setattr(notices, 'capture', lambda *args: {'returncode': 1, 'stdout': '', 'stderr': 'fixture failure'})
    result = notices.audit(app, tmp_path / 'review')
    assert not any(row['path'].endswith('LICENSE-external') for row in result['notice_evidence'])
    assert any(row['code'] == 'media_probe_failed' for row in result['findings'])


def test_probe_failure_is_recorded(tmp_path):
    assert 'error' in notices.capture(tmp_path / 'missing-binary', '-L')


def test_signed_app_cdhash_parses_codesign_output(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(notices.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='', stderr='Executable=/fixture/app\nCDHash=0123456789abcdef\nSignature=adhoc\n'))
    assert notices.app_cdhash(tmp_path / 'Fixture.app') == '0123456789abcdef'


def test_supplement_requires_exact_metadata_and_file_hash(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    dist = app / 'Contents/Resources/python/lib/python3.12/site-packages/example-1.dist-info'
    root = tmp_path / 'supplements'
    root.mkdir()
    (root / 'LICENSE').write_text('Verified supplemental notice')
    entry = {'name': 'example', 'version': '1', 'metadata_sha256': notices.sha256(dist / 'METADATA'),
             'file': 'LICENSE', 'sha256': notices.sha256(root / 'LICENSE')}
    (root / 'provenance.json').write_text(json.dumps({'supplements': [entry]}))
    monkeypatch.setattr(notices, 'SUPPLEMENT_ROOT', root)
    monkeypatch.setattr(notices, 'capture', lambda *args: {'returncode': 0, 'stdout': '', 'stderr': ''})
    result = notices.audit(app, tmp_path / 'first')
    assert len(result['python_distributions'][0]['matching_supplements']) == 1
    assert 'Verified supplemental notice' in (tmp_path / 'first/THIRD_PARTY_NOTICES.txt').read_text()
    (root / 'LICENSE').write_text('tampered')
    result = notices.audit(app, tmp_path / 'second')
    assert not result['python_distributions'][0]['matching_supplements']
    assert any(row['code'] == 'supplement_invalid' for row in result['findings'])
    (dist / 'METADATA').write_text((dist / 'METADATA').read_text() + '\nChanged: yes\n')
    result = notices.audit(app, tmp_path / 'third')
    assert not result['python_distributions'][0]['matching_supplements']
    assert 'tampered' not in (tmp_path / 'third/THIRD_PARTY_NOTICES.txt').read_text()


def test_legacy_license_with_shipped_notice_is_evidence_not_automatic_blocker(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    dist = app / 'Contents/Resources/python/lib/python3.12/site-packages/example-1.dist-info'
    (dist / 'METADATA').write_text('Name: example\nVersion: 1\nLicense: MIT\nLicense-File: LICENSE\n')
    monkeypatch.setattr(notices, 'capture', lambda *args: {'returncode': 0, 'stdout': '', 'stderr': ''})
    result = notices.audit(app, tmp_path / 'review')
    assert result['python_distributions'][0]['license_metadata_basis'] == 'legacy metadata'
    assert not any(f['code'] in {'license_expression_unresolved', 'license_metadata_missing', 'distribution_notice_absent'} for f in result['findings'])
    files = tmp_path / 'review/recipient-notices'
    inputs = json.loads((files / 'notice-inputs.json').read_text())
    assert inputs['distributions'][0]['metadata_sha256'] == notices.sha256(dist / 'METADATA')
    assert inputs['files'][0]['sha256'] == notices.sha256(files / 'THIRD_PARTY_NOTICES.txt')
    assert 'app_cdhash' not in inputs
    assert 'Example permission notice' in (files / 'THIRD_PARTY_NOTICES.txt').read_text()


def test_supplement_comparison_evidence_must_match(tmp_path, monkeypatch):
    app = fixture_app(tmp_path)
    dist = app / 'Contents/Resources/python/lib/python3.12/site-packages/example-1.dist-info'
    root = tmp_path / 'supplements'
    root.mkdir()
    (root / 'LICENSE').write_text('Supplement')
    (root / 'comparison.json').write_text('evidence')
    entry = {'name': 'example', 'version': '1', 'metadata_sha256': notices.sha256(dist / 'METADATA'),
             'file': 'LICENSE', 'sha256': notices.sha256(root / 'LICENSE'),
             'comparison_file': 'comparison.json', 'comparison_sha256': notices.sha256(root / 'comparison.json')}
    (root / 'provenance.json').write_text(json.dumps({'supplements': [entry]}))
    monkeypatch.setattr(notices, 'SUPPLEMENT_ROOT', root)
    monkeypatch.setattr(notices, 'capture', lambda *args: {'returncode': 0, 'stdout': '', 'stderr': ''})
    (root / 'comparison.json').write_text('altered')
    result = notices.audit(app, tmp_path / 'review')
    assert not result['python_distributions'][0]['matching_supplements']
    assert any(row['code'] == 'supplement_invalid' for row in result['findings'])
