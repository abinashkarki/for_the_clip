import hashlib
import importlib.util
import io
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('source_inputs', Path(__file__).resolve().parents[1] / 'scripts/collect_macos_source_inputs.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class Response(io.BytesIO):
    def geturl(self):
        return 'https://example.test/source'


def test_fetch_verifies_and_reuses_without_network(tmp_path, monkeypatch):
    data = b'fixture source'
    item = {'url': 'https://example.test/source', 'filename': 'source.tar', 'sha256': hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(collector.urllib.request, 'urlopen', lambda *a, **k: Response(data))
    assert collector.fetch(item, tmp_path, 100)['status'] == 'verified'
    monkeypatch.setattr(collector.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('redownload'))
    assert collector.fetch(item, tmp_path, 100)['status'] == 'verified_cached'


def test_hash_failure_and_size_limit_preserve_partial(tmp_path, monkeypatch):
    item = {'url': 'https://example.test/source', 'filename': 'source.tar', 'sha256': '0' * 64}
    monkeypatch.setattr(collector.urllib.request, 'urlopen', lambda *a, **k: Response(b'bad bytes'))
    with pytest.raises(ValueError, match='hash mismatch'):
        collector.fetch(item, tmp_path, 100)
    assert (tmp_path / 'source.tar.partial').read_bytes() == b'bad bytes'
    with pytest.raises(ValueError, match='partial exists'):
        collector.fetch(item, tmp_path, 100)
    (tmp_path / 'source.tar.partial').unlink()
    with pytest.raises(ValueError, match='size cap'):
        collector.fetch(item, tmp_path, 2)
    assert (tmp_path / 'source.tar.partial').exists()


def test_requires_https_and_detects_local_evidence_mismatch(tmp_path):
    with pytest.raises(ValueError, match='HTTPS'):
        collector.fetch({'url': 'http://example.test', 'filename': 'x', 'sha256': 'a'}, tmp_path, 100)
    src = tmp_path / 'local'; src.write_bytes(b'abc')
    result = collector.copy_evidence(src, tmp_path / 'copy', tmp_path, 100, 'bad')
    assert result['status'] == 'hash_mismatch'
    assert not (tmp_path / 'copy').exists()


def test_rejects_output_inside_app(tmp_path):
    app = tmp_path / 'Clippie.app'
    with pytest.raises(ValueError, match='outside app'):
        collector.collect({}, app / 'source-inputs', app)
    assert not app.exists()


def test_source_bundle_rejects_changed_input(tmp_path):
    import json
    import tarfile
    bundle_script = Path(__file__).resolve().parents[1] / 'packaging/macos/third-party/source-evidence/assemble_source_bundle.py'
    bundle_spec = importlib.util.spec_from_file_location('assemble_source_bundle', bundle_script)
    bundler = importlib.util.module_from_spec(bundle_spec)
    bundle_spec.loader.exec_module(bundler)
    root = tmp_path / 'source'; root.mkdir()
    (root / 'source.tar').write_bytes(b'source')
    (root / 'x264.tar').write_bytes(b'x264')
    pins = {'downloads': [{'id': 'source', 'filename': 'source.tar', 'sha256': collector.digest(root / 'source.tar')}]}
    (root / 'source-pins.json').write_text(json.dumps(pins))
    data = {'downloads': {'source': {'status': 'verified'}},
            'x264': {'status': 'verified_git_commit', 'file': 'x264.tar', 'sha256': collector.digest(root / 'x264.tar')},
            'evidence': {}, 'installed_notices': {}}
    (root / 'collection.json').write_text(json.dumps(data))
    readme = tmp_path / 'README'; readme.write_text('Evidence only')
    dest = tmp_path / 'bundle.tar'
    bundler.assemble(root, dest, readme)
    with tarfile.open(dest) as archive:
        assert {'source.tar', 'x264.tar', 'README.txt', 'SHA256SUMS'} <= set(archive.getnames())
    (root / 'source.tar').write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed input'):
        bundler.assemble(root, tmp_path / 'invalid.tar', readme)
    assert not (tmp_path / 'invalid.tar').exists()
