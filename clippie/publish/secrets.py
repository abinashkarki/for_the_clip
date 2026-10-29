"""Publishing secrets (OAuth tokens, client secrets, API keys) live only in the macOS Keychain.

SQLite holds a reference name per secret, never the value. Environment variables named
`CLIPPIE_PUBLISH_<NAME>` override the Keychain, matching the model-key pattern in `credentials.py`.
"""
import os

from .. import credentials

SERVICE = 'Clippie publishing'
STORAGE_ERROR = 'Could not access macOS Keychain. Unlock your login keychain or allow Clippie access, then try again.'


class SecretError(RuntimeError):
    pass


def env_name(name):
    return 'CLIPPIE_PUBLISH_' + ''.join(c if c.isalnum() else '_' for c in name).upper()


class SecretStore:
    def get(self, name):
        if env_name(name) in os.environ:
            return os.environ[env_name(name)] or None
        try:
            return credentials.keychain().get_password(SERVICE, name) or None
        except Exception:
            return None

    def origin(self, name):
        if env_name(name) in os.environ:
            return 'environment'
        return 'keychain' if self.get(name) else 'none'

    def set(self, name, value):
        value = (value or '').strip()
        if not value:
            raise ValueError('Enter a value to save.')
        if env_name(name) in os.environ:
            raise SecretError(f'This value is managed by the {env_name(name)} environment variable.')
        try:
            credentials.keychain().set_password(SERVICE, name, value)
        except Exception:
            raise SecretError(STORAGE_ERROR) from None

    def delete(self, name):
        try:
            backend = credentials.keychain()
            if backend.get_password(SERVICE, name) is not None:
                backend.delete_password(SERVICE, name)
        except Exception:
            raise SecretError(STORAGE_ERROR) from None
