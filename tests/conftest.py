"""Tests must never write fixture credentials to the user's real Keychain."""
import os

import pytest

# The lifecycle sweeper deletes media; it must never run against a real data folder from a test app lifespan.
os.environ.setdefault('CLIPPIE_DISABLE_LIFECYCLE_SWEEP', '1')


@pytest.fixture(autouse=True)
def isolated_credential_store(tmp_path, monkeypatch):
    from clippie import credentials, app
    class MemoryKeychain:
        def __init__(self): self.values = {}
        def get_password(self, service, account): return self.values.get((service, account))
        def set_password(self, service, account, value): self.values[service, account] = value
        def delete_password(self, service, account): self.values.pop((service, account), None)
    backend = MemoryKeychain()
    monkeypatch.setattr(credentials, 'keychain', lambda: backend)
    monkeypatch.setattr(app, 'CREDENTIALS', credentials.CredentialStore(tmp_path))
    return backend


@pytest.fixture(autouse=True)
def isolated_model_log(tmp_path_factory):
    """The model call log must never write into the developer's real data directory."""
    from clippie.models import gateway
    gateway.configure(tmp_path_factory.mktemp('model-data'))
    yield
    gateway.configure(None)


@pytest.fixture(autouse=True)
def no_rate_limit_waits(monkeypatch):
    """Rate-limit retries are off in tests unless a test turns them on."""
    from clippie.models import gateway
    monkeypatch.setattr(gateway, 'RATE_LIMIT_WAITS', ())
    monkeypatch.setattr(gateway, 'SLEEP', lambda seconds: None)


def pytest_configure(config):
    config.addinivalue_line('markers', 'opening_judge: run the blind opening judge model call during discovery')


@pytest.fixture(autouse=True)
def opening_judge_off_unless_marked(request, monkeypatch):
    """Fakes written before the blind opening judge answer only discovery; it is off unless a test opts in."""
    if request.node.get_closest_marker('opening_judge'):
        return
    from clippie import collection_editor
    monkeypatch.setattr(collection_editor, 'judge_openings', lambda *args, **kwargs: ({}, {}, ''))
