"""Fernet encryption for refresh tokens at rest. The key comes from CLOUD_FERNET_KEY, never from the database."""
import json

from cryptography.fernet import Fernet, InvalidToken


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, key):
        try:
            self.fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError):
            raise VaultError('CLOUD_FERNET_KEY must be a Fernet key. Generate one with: '
                             'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"') from None

    def seal(self, value):
        return self.fernet.encrypt(json.dumps(value).encode()).decode()

    def open(self, blob):
        try:
            return json.loads(self.fernet.decrypt(blob.encode()))
        except InvalidToken:
            raise VaultError('Stored credentials cannot be decrypted with the current CLOUD_FERNET_KEY.') from None
