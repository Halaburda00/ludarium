from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from ludarium.config import ConfigurationError, Settings, get_settings
from ludarium.crypto import get_cipher


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Settings resolve `.env` against the working directory, and a developer runs
    # pytest from backend/, where a real one lives.
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    # The cipher caches the key it was built from, so it would outlive the
    # settings it came from.
    get_cipher.cache_clear()


def test_settings_read_the_ludarium_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDARIUM_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LUDARIUM_DATABASE_URL", "sqlite+aiosqlite:///./data/other.db")

    settings = get_settings()

    assert settings.log_level == "DEBUG"
    assert settings.database_url == "sqlite+aiosqlite:///./data/other.db"


def test_missing_encryption_key_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUDARIUM_ENCRYPTION_KEY")

    with pytest.raises(ConfigurationError, match="encryption_key"):
        get_settings()


def test_unusable_encryption_key_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDARIUM_ENCRYPTION_KEY", "not-a-fernet-key")

    with pytest.raises(ConfigurationError, match="not a valid Fernet key"):
        get_settings()


def test_secrets_are_masked_in_repr() -> None:
    settings = get_settings()

    rendered = f"{settings!r} {settings.model_dump()}"

    assert settings.encryption_key.get_secret_value() not in rendered
    assert settings.secret_key.get_secret_value() not in rendered


def test_explicit_values_win_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDARIUM_LOG_LEVEL", "ERROR")

    settings = Settings(secret_key=SecretStr("s"), log_level="WARNING")

    assert settings.log_level == "WARNING"


def test_encryption_key_validation_does_not_echo_the_key() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(secret_key=SecretStr("s"), encryption_key=SecretStr("wrong-but-secret"))

    assert "wrong-but-secret" not in str(exc_info.value)


def test_startup_failure_does_not_echo_other_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDARIUM_SECRET_KEY", "a-real-session-secret")
    monkeypatch.delenv("LUDARIUM_ENCRYPTION_KEY")

    with pytest.raises(ConfigurationError) as exc_info:
        get_settings()

    assert "a-real-session-secret" not in str(exc_info.value)
