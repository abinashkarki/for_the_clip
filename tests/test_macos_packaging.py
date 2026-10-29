"""Fail-closed native dependency audit checks, without downloading/building."""
import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location('package_macos', Path(__file__).resolve().parents[1] / 'scripts/package_macos.py')
packaging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packaging)


def test_dylib_identity_is_not_a_load_dependency(monkeypatch):
    sample = '''Load command 1
          cmd LC_ID_DYLIB
         name /builder/libexample.dylib (offset 24)
Load command 2
          cmd LC_LOAD_DYLIB
         name @loader_path/libdependency.dylib (offset 24)
Load command 3
          cmd LC_RPATH
         path @loader_path/lib (offset 12)
Load command 4
          cmd LC_BUILD_VERSION
        minos 14.0
'''
    monkeypatch.setattr(packaging, 'run', lambda *a, **kw: SimpleNamespace(stdout=sample))
    metadata = {}
    dependencies, rpaths, identity = packaging.load_commands(Path('fixture'), metadata=metadata)
    assert dependencies == ['@loader_path/libdependency.dylib']
    assert rpaths == ['@loader_path/lib']
    assert identity == '/builder/libexample.dylib'
    assert metadata == {'minimum_macos': ['14.0']}


def fake_bundle(tmp_path, monkeypatch, dependencies, rpaths=()):
    app = tmp_path / 'Relocated.app'
    executable = app / 'Contents/Resources/python/bin/python3'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'\xcf\xfa\xed\xfe')
    monkeypatch.setattr(packaging, 'load_commands', lambda *a, **kw: (list(dependencies), list(rpaths), None))
    monkeypatch.setattr(packaging, 'run', lambda *a, **kw: SimpleNamespace(stdout='arm64\n'))
    return app, executable


def test_unresolved_rpath_is_a_release_failure(tmp_path, monkeypatch):
    app, _ = fake_bundle(tmp_path, monkeypatch, ['@rpath/absent.dylib'], ['@loader_path/../lib'])
    with pytest.raises(RuntimeError, match='Unresolved/external'):
        packaging.audit(app)


def test_existing_loader_dependency_outside_app_is_rejected(tmp_path, monkeypatch):
    outside = tmp_path / 'developer-library.dylib'
    outside.touch()
    app, executable = fake_bundle(tmp_path, monkeypatch, [str(outside)])
    with pytest.raises(RuntimeError, match='Unresolved/external'):
        packaging.audit(app)


def test_developer_rpath_is_rejected_even_with_no_loads(tmp_path, monkeypatch):
    app, _ = fake_bundle(tmp_path, monkeypatch, [], [str(tmp_path / 'developer-libraries')])
    with pytest.raises(RuntimeError, match='External RPATH'):
        packaging.audit(app)


def test_external_symlink_is_rejected(tmp_path, monkeypatch):
    app, _ = fake_bundle(tmp_path, monkeypatch, [])
    outside = tmp_path / 'outside'
    outside.touch()
    (app / 'external-link').symlink_to(outside)
    with pytest.raises(RuntimeError, match='External symlink'):
        packaging.audit(app)


def test_recipient_notice_inputs_match_runtime_before_copy(tmp_path):
    app = tmp_path / 'Clippie.app'
    metadata = app / 'Contents/Resources/python/example.dist-info/METADATA'
    metadata.parent.mkdir(parents=True)
    metadata.write_text('Name: example\nVersion: 1\n')
    notices = tmp_path / 'notices'
    notices.mkdir()
    (notices / 'THIRD_PARTY_NOTICES.txt').write_text('License fixture')
    hashed = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    record = {'schema_version': 1, 'distributions': [{'metadata_path': str(metadata.relative_to(app)), 'metadata_sha256': hashed(metadata)}],
              'files': [{'path': 'THIRD_PARTY_NOTICES.txt', 'sha256': hashed(notices / 'THIRD_PARTY_NOTICES.txt')}]}
    (notices / 'notice-inputs.json').write_text(json.dumps(record))
    result = packaging.copy_notice_inputs(app, notices)
    assert (app / result['files'][0]['path']).read_text() == 'License fixture'
    metadata.write_text('Name: different\n')
    with pytest.raises(ValueError, match='does not match'):
        packaging.copy_notice_inputs(app, notices)


def test_release_versions_are_concrete_and_preserve_bundle_identity():
    info = {'CFBundleIdentifier': 'app.clippie.desktop.spike'}
    packaging.set_release_version(info, '1.2.3', '42')
    assert info == {'CFBundleIdentifier': 'app.clippie.desktop.spike',
                    'CFBundleShortVersionString': '1.2.3', 'CFBundleVersion': '42'}
    for version, build in [('1.2', '1'), ('1.2.3-beta', '1'), ('1.2.3', ''), ('1.2.3', 'a')]:
        with pytest.raises(ValueError):
            packaging.set_release_version({}, version, build)
