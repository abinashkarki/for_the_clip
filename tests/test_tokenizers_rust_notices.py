"""Crate notice evidence must be bound to the source lock checksum."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile

SPEC = importlib.util.spec_from_file_location('collect_tokenizers_rust_notices', Path(__file__).resolve().parents[1] / 'scripts/collect_tokenizers_rust_notices.py')
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def crate_bytes():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as tar:
        for path, content in {'fixture-1/Cargo.toml': b'[package]\nname="fixture"\nversion="1"\nlicense="MIT"\n',
                              'fixture-1/LICENSE': b'Permission notice'}.items():
            entry = tarfile.TarInfo(path)
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def test_crate_evidence_requires_lock_hash_and_extracts_license(tmp_path, monkeypatch):
    raw = crate_bytes()
    monkeypatch.setattr(collector, 'urlopen', lambda *args, **kwargs: io.BytesIO(raw))
    pkg = {'name': 'fixture', 'version': '1', 'source': 'registry+https://github.com/rust-lang/crates.io-index',
           'checksum': hashlib.sha256(raw).hexdigest()}
    result = collector.fetch(pkg, tmp_path)
    assert result['status'] == 'verified' and result['declared_license'] == 'MIT'
    assert result['notices']['LICENSE']['text'] == 'Permission notice'
    pkg['checksum'] = '0' * 64
    result = collector.fetch(pkg, tmp_path)
    assert result['status'] == 'error' and 'SHA256 mismatch' in result['detail']
