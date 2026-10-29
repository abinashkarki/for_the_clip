"""Local credential configuration; Settings persists only to macOS Keychain."""
from functools import lru_cache
import os
from pathlib import Path
import sys

NAMES = {'deepseek': 'DEEPSEEK_API_KEY', 'groq': 'GROQ_API_KEY', 'gemini': 'GEMINI_API_KEY',
         'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY',
         'nvidia': 'NVIDIA_API_KEY', 'openrouter': 'OPENROUTER_API_KEY'}
SERVICE = 'Clippie API keys'
STORAGE_ERROR = 'Could not access macOS Keychain. Unlock your login keychain or allow Clippie access, then try again. The key was not saved.'


class CredentialStorageError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def keychain():
    if sys.platform != 'darwin':
        raise CredentialStorageError('Persistent Settings requires macOS Keychain. Use a private .env file on this platform.')
    # Select the native backend explicitly. Never auto-select a plaintext plugin.
    from keyring.backends.macOS import Keyring
    return Keyring()


def env_file(root):
    values = {}
    try:
        for line in (Path(root)/'.env').read_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            name, value = line.split('=', 1)
            if name.strip() not in NAMES.values():
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            values[name.strip()] = value
    except OSError:
        pass
    return values


class CredentialStore:
    def __init__(self, root):
        self.root = Path(root)
        self.origins = {}
        self.warning = None

    def load(self):
        values = env_file(self.root)
        keys = {}
        self.warning = None
        backend = None
        try:
            backend = keychain()
        except Exception:
            self.warning = 'macOS Keychain is unavailable. Private environment configuration can still be used.'
        for provider, name in NAMES.items():
            saved = None
            if backend is not None:
                try:
                    saved = backend.get_password(SERVICE, provider)
                except Exception:
                    self.warning = 'macOS Keychain could not be read. Unlock it or allow access, then restart Clippie.'
                    backend = None
            if name in os.environ:
                keys[provider], self.origins[provider] = os.environ[name], 'environment'
            elif saved:
                keys[provider], self.origins[provider] = saved, 'keychain'
            elif values.get(name):
                keys[provider], self.origins[provider] = values[name], 'env_file'
            else:
                keys[provider], self.origins[provider] = '', 'none'
        return keys

    def save(self, provider, value):
        if provider not in NAMES:
            raise ValueError('Unknown credential provider.')
        if NAMES[provider] in os.environ:
            raise CredentialStorageError('This key is managed by an environment variable. Update or remove that variable before changing it in Settings.')
        value = value.strip()
        if not value:
            raise ValueError('Enter a key to save, or use Remove saved key.')
        try:
            keychain().set_password(SERVICE, provider, value)
        except Exception:
            raise CredentialStorageError(STORAGE_ERROR) from None
        self.origins[provider] = 'keychain'
        self.warning = None
        return value

    def remove(self, provider):
        if provider not in NAMES:
            raise ValueError('Unknown credential provider.')
        if self.origins.get(provider) in {'environment', 'env_file'}:
            raise CredentialStorageError('This key is managed outside Settings. Remove it from the environment or private .env file, then restart Clippie.')
        try:
            backend = keychain()
            if backend.get_password(SERVICE, provider) is not None:
                backend.delete_password(SERVICE, provider)
        except Exception:
            raise CredentialStorageError('Could not remove the saved key from macOS Keychain. Unlock it or allow access, then try again.') from None
        return self.load()[provider]


def load_keys(root):
    return CredentialStore(root).load()
