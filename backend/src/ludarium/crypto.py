from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from ludarium.config import get_settings


class CredentialDecryptionError(Exception):
    """A stored credential could not be decrypted with the configured key.

    Carries no payload: the message ends up in logs, the ciphertext must not.
    """


class CredentialCipher:
    """Fernet over platform credentials (architecture rule 7)."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken as exc:
            raise CredentialDecryptionError(
                "stored credential could not be decrypted; "
                "LUDARIUM_ENCRYPTION_KEY has probably changed"
            ) from exc

    def __repr__(self) -> str:
        return f"{type(self).__name__}(key=***)"


@lru_cache
def get_cipher() -> CredentialCipher:
    # Cached for the life of the process, like the settings it reads: the key
    # arrives in the environment (rule 7), and changing that means a restart.
    return CredentialCipher(get_settings().encryption_key.get_secret_value())
