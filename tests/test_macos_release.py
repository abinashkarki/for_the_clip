"""Preflight must not mistake local development signatures for distribution."""
import importlib.util
import json
from pathlib import Path
import plistlib

SPEC = importlib.util.spec_from_file_location('release_check', Path(__file__).resolve().parents[1] / 'scripts/check_macos_release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def fixture(tmp_path):
    app = tmp_path / 'Clippie.app'
    (app / 'Contents/Resources').mkdir(parents=True)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'app.owner.clippie'}))
    (app / 'Contents/Resources/build-manifest.json').write_text(json.dumps({
        'dirty_checkout': False, 'revision': 'commit', 'native_inventory': [{}], 'native_minimum_macos': '26.2'}))
    notices = tmp_path / 'inventory.json'
    notices.write_text(json.dumps({'schema_version': 1, 'app_cdhash': 'a' * 40, 'redistribution_review_complete': True, 'blockers': []}))
    dmg = tmp_path / 'Clippie.dmg'
    dmg.write_bytes(b'installer')
    return app, notices, dmg


def good(arguments):
    detail = ('Authority=Developer ID Application: Fixture (TEAM)\nTeamIdentifier=TEAM\n'
              'Timestamp=Oct 27, 2026\nCDHash=' + 'a' * 40 + '\nCodeDirectory v=20500 flags=0x10000(runtime) hashes=1')
    return {'passed': True, 'exit_code': 0, 'detail': detail}


def test_development_authority_is_not_distribution():
    assert not release.signature_details('Authority=Apple Development: Fixture\n')['developer_id']
    assert not release.signature_details('Signed Time=Oct 27, 2026\n')['timestamp']


def test_missing_team_and_review_block_even_other_checks_pass(tmp_path):
    app, _, dmg = fixture(tmp_path)
    report = release.collect(app, dmg=dmg, run=good)
    assert not report['preflight_passed']
    failures = {item['id'] for item in report['checks'] if not item['passed']}
    assert {'distribution_team', 'redistribution_review', 'dmg_developer_id'} <= failures


def test_ticket_failure_is_not_overridden_by_manifest_claim(tmp_path):
    app, notices, dmg = fixture(tmp_path)
    def no_ticket(arguments):
        return {'passed': False, 'exit_code': 65, 'detail': 'No ticket'} if 'stapler' in arguments else good(arguments)
    report = release.collect(app, dmg=dmg, notices=notices, expected_team='TEAM', run=no_ticket)
    assert not report['preflight_passed']
    assert not report['independent_beta_acceptance']


def test_passing_mechanical_checks_do_not_claim_human_acceptance(tmp_path):
    app, notices, dmg = fixture(tmp_path)
    report = release.collect(app, dmg=dmg, notices=notices, expected_team='TEAM', run=good)
    assert report['preflight_passed']
    assert report['installer_sha256']
    assert not report['independent_beta_acceptance']


def test_review_of_a_different_signed_app_is_rejected(tmp_path):
    app, notices, dmg = fixture(tmp_path)
    value = json.loads(notices.read_text())
    value['app_cdhash'] = 'b' * 40
    notices.write_text(json.dumps(value))
    report = release.collect(app, dmg=dmg, notices=notices, expected_team='TEAM', run=good)
    assert not report['preflight_passed']
    assert not next(item for item in report['checks'] if item['id'] == 'review_matches_app')['passed']
