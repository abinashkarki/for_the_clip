"""Image cleanup must not eject another image or a user-mounted volume."""
import importlib.util
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location('package_macos_dmg', Path(__file__).resolve().parents[1] / 'scripts/package_macos_dmg.py')
dmg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dmg)


def fixture(output, **patch):
    return {'image-path': str(output), 'owner-uid': os.getuid(), 'system-image': True, 'writeable': True,
            'system-entities': [{'dev-entry': '/dev/disk99', 'content-hint': 'GUID_partition_scheme'}], **patch}


def intercept(monkeypatch, images):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=plistlib.dumps({'images': images}))
    monkeypatch.setattr(dmg.subprocess, 'run', run)
    return calls


def test_only_the_own_new_unmounted_creation_driver_is_ejected(tmp_path, monkeypatch):
    output = tmp_path / 'prototype.dmg'
    calls = intercept(monkeypatch, [fixture(tmp_path / 'another.dmg'), fixture(output)])
    dmg.detach_creation_image(output)
    assert calls == [['/usr/bin/hdiutil', 'info', '-plist'], ['/usr/sbin/diskutil', 'eject', '/dev/disk99']]


def test_another_users_image_is_not_ejected(tmp_path, monkeypatch):
    output = tmp_path / 'prototype.dmg'
    calls = intercept(monkeypatch, [fixture(output, **{'owner-uid': os.getuid() + 1})])
    dmg.detach_creation_image(output)
    assert len(calls) == 1


def test_mounted_image_is_not_ejected(tmp_path, monkeypatch):
    output = tmp_path / 'prototype.dmg'
    calls = intercept(monkeypatch, [fixture(output, **{'system-entities': [{'dev-entry': '/dev/disk99', 'mount-point': '/Volumes/Prototype'}]})])
    with pytest.raises(RuntimeError, match='mounted volume'):
        dmg.detach_creation_image(output)
    assert len(calls) == 1


def test_non_creation_driver_is_not_ejected(tmp_path, monkeypatch):
    output = tmp_path / 'prototype.dmg'
    calls = intercept(monkeypatch, [fixture(output, writeable=False)])
    with pytest.raises(RuntimeError, match='outside its creation'):
        dmg.detach_creation_image(output)
    assert len(calls) == 1
