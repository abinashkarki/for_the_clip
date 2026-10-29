import pytest
from clippie import credentials
from clippie.credentials import CredentialStore, CredentialStorageError


def test_keychain_survives_new_store_and_replaces_and_removes(tmp_path, monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    first = CredentialStore(tmp_path)
    first.save('gemini', 'private-key')
    second = CredentialStore(tmp_path)
    assert second.load()['gemini'] == 'private-key'
    assert second.origins['gemini'] == 'keychain'
    second.save('gemini', 'replacement')
    assert CredentialStore(tmp_path).load()['gemini'] == 'replacement'
    assert second.remove('gemini') == ''
    assert CredentialStore(tmp_path).load()['gemini'] == ''


def test_precedence_and_external_key_management(tmp_path, monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    (tmp_path/'.env').write_text('GEMINI_API_KEY="literal-$(not-executed)"\n')
    store = CredentialStore(tmp_path)
    assert store.load()['gemini'] == 'literal-$(not-executed)'
    assert store.origins['gemini'] == 'env_file'
    store.save('gemini', 'keychain-value')
    assert store.load()['gemini'] == 'keychain-value'
    monkeypatch.setenv('GEMINI_API_KEY', 'environment-value')
    assert store.load()['gemini'] == 'environment-value'
    with pytest.raises(CredentialStorageError, match='environment variable'):
        store.save('gemini', 'replacement')
    with pytest.raises(CredentialStorageError, match='managed outside'):
        store.remove('gemini')


def test_unavailable_keychain_never_silently_saves_plaintext(tmp_path, monkeypatch):
    def unavailable(): raise RuntimeError('secret-sensitive-backend-error')
    monkeypatch.setattr(credentials, 'keychain', unavailable)
    store = CredentialStore(tmp_path)
    store.load()
    assert store.warning
    with pytest.raises(CredentialStorageError) as error:
        store.save('gemini', 'private-key')
    assert 'private-key' not in str(error.value) and 'secret-sensitive' not in str(error.value)
    assert not (tmp_path/'.env').exists()
