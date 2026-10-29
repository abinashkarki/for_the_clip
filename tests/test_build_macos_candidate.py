"""The candidate wrapper rejects stale inputs and mismatched packaged assets."""
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('build_macos_candidate', Path(__file__).resolve().parents[1] / 'scripts/build_macos_candidate.py')
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)


def setup_root(tmp_path):
    root = tmp_path / 'repo'
    for name in ['web/src', 'clippie/static/studio', 'packaging/macos', 'scripts']:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in ['README.md', 'pyproject.toml', 'uv.lock', 'scripts/package_macos.py', 'web/src/ui.ts']:
        (root / name).write_text(name)
    return root


def args(tmp_path):
    return SimpleNamespace(work_dir=tmp_path / 'work', app_version='1.2.3', build_number='7',
                           uv='uv', identity='-', ffmpeg=None, ffprobe=None, notices_dir=None, timestamp=False)


def executor(root, work, *, change=None, stale=False):
    def run(command, cwd, check):
        if command[0] == 'npm':
            (root / 'clippie/static/studio/index.html').write_text('fresh frontend')
            if change == 'frontend': (root / 'web/src/ui.ts').write_text('changed during build')
            if change == 'backend_frontend': (root / 'clippie/source.py').write_text('changed during frontend build')
            return
        app = work / 'Clippie.app'
        resource = app / 'Contents/Resources'
        resource.mkdir(parents=True)
        shutil.copytree(root / 'clippie/static/studio', resource / 'app/clippie/static/studio')
        if stale: (resource / 'app/clippie/static/studio/index.html').write_text('old frontend')
        provenance = json.loads((work / 'frontend-provenance.json').read_text())
        (resource / 'build-manifest.json').write_text(json.dumps({'app_version': '1.2.3', 'build_number': '7', 'frontend_provenance': provenance}))
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '1.2.3', 'CFBundleVersion': '7'}))
        if change == 'backend': (root / 'clippie/source.py').write_text('concurrent change')
    return run


def test_candidate_records_rebuilt_frontend_and_matching_version(tmp_path):
    root = setup_root(tmp_path); opts = args(tmp_path)
    app = wrapper.build(opts, root=root, execute=executor(root, opts.work_dir))
    record = wrapper.verify_candidate(app, json.loads((opts.work_dir / 'frontend-provenance.json').read_text()))
    assert record['app_version'] == '1.2.3' and record['build_number'] == '7'
    assert record['frontend_provenance']['inputs_sha256'] and record['frontend_provenance']['assets_sha256']


@pytest.mark.parametrize('change,stale,error', [('frontend', False, 'Frontend inputs changed'),
    ('backend_frontend', False, 'Source changed during the frontend build'),
    ('backend', False, 'Source changed during packaging'), (None, True, 'frontend assets')])
def test_changed_sources_or_stale_assets_refuse_candidate(tmp_path, change, stale, error):
    root = setup_root(tmp_path); opts = args(tmp_path)
    with pytest.raises(ValueError, match=error):
        wrapper.build(opts, root=root, execute=executor(root, opts.work_dir, change=change, stale=stale))
    assert not (opts.work_dir / 'Clippie.app').exists()
    if change not in ('frontend', 'backend_frontend'): assert list(opts.work_dir.glob('Clippie.invalid-*.app'))


def test_runtime_version_comes_from_bundle_not_writable_data(tmp_path, monkeypatch):
    from clippie import paths
    package = tmp_path / 'Clippie.app/Contents/Resources/app/clippie'
    package.mkdir(parents=True)
    (package / 'paths.py').touch()
    (package.parent.parent / 'build-manifest.json').write_text(json.dumps({'app_version': '1.2.3'}))
    monkeypatch.setattr(paths, '__file__', str(package / 'paths.py'))
    monkeypatch.setenv('CLIPPIE_DATA_DIR', str(tmp_path / 'elsewhere'))
    assert paths.app_version() == '1.2.3'
    (package.parent.parent / 'build-manifest.json').unlink()
    assert paths.app_version() == '0.1.0'


def test_health_reports_packaged_release_version(monkeypatch):
    from clippie import app as api
    monkeypatch.setattr(api, 'app_version', lambda: '1.2.3')
    monkeypatch.setattr(api.shutil, 'which', lambda _: None)
    monkeypatch.setattr(api, 'local_models', lambda: {})
    monkeypatch.setattr(api, 'settings', lambda: {})
    assert api.health()['version'] == '1.2.3'


def test_existing_candidate_is_preserved(tmp_path):
    root = setup_root(tmp_path); opts = args(tmp_path)
    app = opts.work_dir / 'Clippie.app'; app.mkdir(parents=True)
    (app / 'preserve').write_text('old candidate')
    with pytest.raises(ValueError, match='already contains'):
        wrapper.build(opts, root=root, execute=lambda *args, **kwargs: pytest.fail('must not start build'))
    assert (app / 'preserve').read_text() == 'old candidate'
