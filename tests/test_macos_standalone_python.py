"""The supplemental runtime notice preserves its pinned evidence and limitations."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'packaging/macos/third-party/standalone-python'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_notices_match_pinned_text_and_release_metadata():
    provenance = json.loads((ROOT / 'provenance.json').read_text())
    notice = (ROOT / 'THIRD_PARTY_NOTICES.txt').read_bytes()
    assert sha(ROOT / 'PYTHON.json') == provenance['python_json_sha256']
    assert sha(ROOT / 'THIRD_PARTY_NOTICES.txt') == provenance['notices_sha256']
    assert sha(ROOT / 'install-match-evidence.json') == provenance['install_evidence_sha256']
    assert {'LICENSE.thread-2.8.9.txt', 'LICENSE.itcl-4.2.4.txt'} <= set(provenance['license_files'])
    for name, expected in provenance['license_files'].items():
        path = ROOT / 'licenses' / name
        assert sha(path) == expected
        assert path.read_bytes() in notice
    data = json.loads((ROOT / 'PYTHON.json').read_text())
    assert data['python_version'] == provenance['python_version'] == '3.12.12'
    assert data['target_triple'] == provenance['target']
    assert data['build_info']['extensions']['_tkinter'][0]['license_paths'] == ['licenses/LICENSE.tcl.txt']


def test_runtime_matching_evidence_keeps_rewritten_binaries_distinct():
    evidence = json.loads((ROOT / 'install-match-evidence.json').read_text())
    by_path = {row['path']: row for row in evidence}
    for path in ('lib/libtcl8.6.dylib', 'lib/libtk8.6.dylib',
                 'lib/thread2.8.9/libthread2.8.9.dylib', 'lib/itcl4.2.4/libitcl4.2.4.dylib'):
        assert by_path[path]['toolchain_matches_stripped'] is True
        assert by_path[path]['bundle_matches_stripped'] is False


def test_libpython_sections_prove_payload_correspondence():
    provenance = json.loads((ROOT / 'provenance.json').read_text())
    evidence_file = ROOT / 'libpython-macho-evidence.json'
    assert sha(evidence_file) == provenance['libpython_macho_evidence_sha256']
    evidence = json.loads(evidence_file.read_text())
    assert evidence['all_section_payloads_identical']
    assert len(evidence['release']['sections']) == 15
    assert evidence['release']['sections'] == evidence['toolchain']['sections'] == evidence['app']['sections']
    assert 'unsectioned' in evidence['qualification']
