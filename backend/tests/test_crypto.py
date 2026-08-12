import logging

import pytest
from conftest import TEST_ENCRYPTION_KEY
from cryptography.fernet import Fernet

from ludarium.crypto import CredentialCipher, CredentialDecryptionError, get_cipher

SECRET = "steam-api-key-0123456789ABCDEF"


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(Fernet.generate_key().decode())


def test_round_trip(cipher: CredentialCipher) -> None:
    assert cipher.decrypt(cipher.encrypt(SECRET)) == SECRET


def test_the_configured_key_is_the_one_in_use() -> None:
    cipher = get_cipher()

    assert cipher.decrypt(cipher.encrypt(SECRET)) == SECRET
    assert CredentialCipher(TEST_ENCRYPTION_KEY).decrypt(cipher.encrypt(SECRET)) == SECRET


def test_ciphertext_does_not_contain_the_plaintext(cipher: CredentialCipher) -> None:
    token = cipher.encrypt(SECRET)

    assert SECRET.encode() not in token
    # Fernet embeds a random IV, so the same input never yields the same token.
    assert token != cipher.encrypt(SECRET)


def test_decrypting_with_another_key_is_refused(cipher: CredentialCipher) -> None:
    other = CredentialCipher(Fernet.generate_key().decode())

    with pytest.raises(CredentialDecryptionError):
        other.decrypt(cipher.encrypt(SECRET))


def test_nothing_secret_reaches_a_log_record(
    cipher: CredentialCipher, caplog: pytest.LogCaptureFixture
) -> None:
    token = cipher.encrypt(SECRET)

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("ludarium").debug("cipher in use: %r", cipher)
        with pytest.raises(CredentialDecryptionError) as exc_info:
            CredentialCipher(Fernet.generate_key().decode()).decrypt(token)
        logging.getLogger("ludarium").exception("decrypt failed", exc_info=exc_info.value)

    assert SECRET not in caplog.text
    assert token.decode() not in caplog.text
