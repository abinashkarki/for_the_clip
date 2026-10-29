"""Private testing must preserve signature, runtime and redistribution evidence."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_macos_close_testers as alpha


def fixture(tmp_path):
    app = tmp_path / 'Clippie.app'
    for name in ('Contents/MacOS/Clippie', 'Contents/Resources/python/bin/python3',
                 'Contents/Resources/bin/ffmpeg', 'Contents/Resources/bin/ffprobe'):
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
        path.chmod(0o755)
    (app / 'Contents/Resources/build-manifest.json').write_text(json.dumps({
        'revision': 'abc', 'dirty_checkout': True, 'native_minimum_macos': '26.2',
        'native_inventory': [{'path': 'Contents/MacOS/Clippie'}]}))
    review = tmp_path / 'review.json'
    review.write_text(json.dumps({'schema_version': 1, 'app_cdhash': 'a' * 40,
                                  'technical_materials_review_complete': True, 'blockers': []}))
    items = []
    for kind in ('notice', 'source', 'source_evidence'):
        path = tmp_path / ('source.zip' if kind == 'source' else kind + '.txt')
        if kind == 'source':
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('source.c', 'fixture source')
        else:
            path.write_text(kind)
        items.append({'kind': kind, 'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    review_data = json.loads(review.read_text())
    review_data['delivery_requirements'] = [{'component': 'fixture', 'files': [item['path'] for item in items]}]
    review.write_text(json.dumps(review_data))
    delivery = tmp_path / 'delivery.json'
    delivery.write_text(json.dumps({'schema_version': 1, 'app_cdhash': 'a' * 40,
                                    'source_obligations_reviewed': True, 'files': items}))
    return app, review, delivery


def signed(args):
    return {'passed': True, 'detail': 'Authority=Apple Development: Fixture\nCDHash=' + 'a' * 40}


def failures(report):
    return {c['id'] for c in report['checks'] if not c['passed']}


def test_development_identity_can_pass_but_install_is_not_accepted(tmp_path):
    app, review, delivery = fixture(tmp_path)
    report = alpha.collect(app, review=review, delivery=delivery, run=signed)
    assert report['assisted_alpha_preflight_passed']
    assert report['independent_install_accepted'] is False
    assert 'unnotarized' in report['warning']


def test_missing_review_and_delivery_fail(tmp_path):
    app, _, _ = fixture(tmp_path)
    assert {'redistribution_review', 'review_matches_app', 'delivery_evidence'} <= failures(alpha.collect(app, run=signed))


def test_modified_delivery_file_and_unresolved_review_fail(tmp_path):
    app, review, delivery = fixture(tmp_path)
    (tmp_path / 'notice.txt').write_text('changed')
    data = json.loads(review.read_text())
    data['blockers'] = ['source']
    review.write_text(json.dumps(data))
    assert {'delivery_evidence', 'redistribution_review'} <= failures(alpha.collect(app, review=review, delivery=delivery, run=signed))


def test_runtime_symlink_escape_and_bad_signature_fail(tmp_path):
    app, review, delivery = fixture(tmp_path)
    python = app / 'Contents/Resources/python/bin/python3'
    python.unlink()
    python.symlink_to('/bin/sh')
    def invalid(args):
        return {'passed': False, 'detail': 'invalid'}
    assert {'app_signature', 'app_identity', 'owned_runtime'} <= failures(alpha.collect(app, review=review, delivery=delivery, run=invalid))


def test_delivery_must_be_bound_and_stay_inside_evidence_folder(tmp_path):
    app, review, delivery = fixture(tmp_path)
    data = json.loads(delivery.read_text())
    data['app_cdhash'] = 'b' * 40
    data['files'][0]['path'] = '../notice.txt'
    delivery.write_text(json.dumps(data))
    assert 'delivery_evidence' in failures(alpha.collect(app, review=review, delivery=delivery, run=signed))


def test_alpha_staging_copies_hashed_materials_and_rejects_incomplete_review(tmp_path):
    import package_macos_dmg as dmg
    import pytest
    app, review, delivery = fixture(tmp_path)
    staging = tmp_path / 'stage'
    staging.mkdir()
    report, records = dmg.stage_close_tester_materials(app, review, delivery, staging, run=signed)
    assert report['assisted_alpha_preflight_passed']
    assert {item['kind'] for item in records} == {'notice', 'source', 'source_evidence'}
    for item in records:
        assert hashlib.sha256((staging / item['path']).read_bytes()).hexdigest() == item['sha256']
    bad = json.loads(review.read_text())
    bad['blockers'] = ['unresolved source']
    review.write_text(json.dumps(bad))
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(ValueError, match='redistribution_review'):
        dmg.stage_close_tester_materials(app, review, delivery, other, run=signed)
    assert not list(other.iterdir())


def test_alpha_staging_rejects_duplicate_or_unnormalized_paths(tmp_path):
    import package_macos_dmg as dmg
    import pytest
    app, review, delivery = fixture(tmp_path)
    data = json.loads(delivery.read_text())
    data['files'].append(dict(data['files'][0]))
    delivery.write_text(json.dumps(data))
    staging = tmp_path / 'stage'
    staging.mkdir()
    with pytest.raises(ValueError, match='unique, normalized'):
        dmg.stage_close_tester_materials(app, review, delivery, staging, run=signed)


def test_alpha_staging_rejects_relative_path_aliases(tmp_path):
    import package_macos_dmg as dmg
    import pytest
    app, review, delivery = fixture(tmp_path)
    data = json.loads(delivery.read_text())
    for index, alias in enumerate(('./notice.txt', 'notice.txt/')):
        data['files'][0]['path'] = alias
        delivery.write_text(json.dumps(data))
        mapped = json.loads(review.read_text())
        mapped['delivery_requirements'][0]['files'][0] = alias
        review.write_text(json.dumps(mapped))
        staging = tmp_path / f'stage-{index}'
        staging.mkdir()
        with pytest.raises(ValueError, match='unique, normalized'):
            dmg.stage_close_tester_materials(app, review, delivery, staging, run=signed)


def test_malformed_evidence_objects_fail_cleanly(tmp_path):
    import pytest
    app, review, delivery = fixture(tmp_path)
    original = review.read_text()
    review.write_text('[]')
    with pytest.raises(ValueError, match='Review inventory'):
        alpha.collect(app, review=review, delivery=delivery, run=signed)
    review.write_text(original)
    delivery.write_text('[]')
    with pytest.raises(ValueError, match='Delivery manifest'):
        alpha.collect(app, review=review, delivery=delivery, run=signed)


def test_delivery_mapping_and_source_bytes_are_required(tmp_path):
    app, review, delivery = fixture(tmp_path)
    data = json.loads(review.read_text())
    del data['delivery_requirements']
    review.write_text(json.dumps(data))
    (tmp_path / 'source.zip').write_bytes(b'')
    assert {'reviewed_delivery_mapping', 'delivery_evidence'} <= failures(alpha.collect(app, review=review, delivery=delivery, run=signed))


def test_separate_delivery_is_hashed_and_not_staged(tmp_path):
    import package_macos_dmg as dmg
    app, review, delivery = fixture(tmp_path)
    data = json.loads(delivery.read_text())
    data['files'][1]['placement'] = 'separate'
    delivery.write_text(json.dumps(data))
    staging = tmp_path / 'stage'
    staging.mkdir()
    _, records = dmg.stage_close_tester_materials(app, review, delivery, staging, run=signed)
    source = next(row for row in records if row['kind'] == 'source')
    assert source['placement'] == 'separate'
    assert not (staging / 'Notices and Source Information' / 'source.zip').exists()
    assert source['sha256'] == hashlib.sha256((tmp_path / 'source.zip').read_bytes()).hexdigest()


def test_downloaded_image_verifier_detects_changes_and_wrong_mount(tmp_path):
    import verify_macos_close_tester_image as image
    import package_macos_dmg as dmg
    app, review, delivery = fixture(tmp_path)
    mount = tmp_path / 'mounted'
    mount.mkdir()
    import shutil
    shutil.copytree(app, mount / 'Clippie.app')
    _, files = dmg.stage_close_tester_materials(mount / 'Clippie.app', review, delivery, mount, run=signed)
    (mount / 'READ BEFORE USE.txt').write_text('UNNOTARIZED')
    disk = tmp_path / 'download.dmg'
    disk.write_bytes(b'dmg')
    sha = hashlib.sha256(disk.read_bytes()).hexdigest()
    sidecar = tmp_path / 'sidecar.json'
    sidecar.write_text(json.dumps({'sha256': sha, 'app_cdhash': 'a' * 40, 'staged_delivery_files': files}))
    report = image.inspect(disk, mount, sidecar, sha, run=signed, quarantine=True, mount_check=lambda *_: True)
    assert report['mechanical_inspection_passed'] and not report['independent_install_accepted']
    (mount / files[0]['path']).write_text('tampered')
    report = image.inspect(disk, mount, sidecar, sha, run=signed, quarantine=False, mount_check=lambda *_: False)
    assert {'delivery_contents', 'mounted_image', 'download_quarantined'} <= failures(report)


def test_stage_rejects_case_insensitive_path_collision(tmp_path):
    import package_macos_dmg as dmg
    import pytest
    app, review, delivery = fixture(tmp_path)
    data = json.loads(delivery.read_text())
    duplicate = dict(data['files'][0], path='NOTICE.TXT')
    (tmp_path / 'NOTICE.TXT').write_text('notice')
    data['files'].append(duplicate)
    delivery.write_text(json.dumps(data))
    mapping = json.loads(review.read_text())
    mapping['delivery_requirements'][0]['files'].append('NOTICE.TXT')
    review.write_text(json.dumps(mapping))
    staging = tmp_path / 'stage'
    staging.mkdir()
    with pytest.raises(ValueError, match='unique, normalized'):
        dmg.stage_close_tester_materials(app, review, delivery, staging, run=signed)
