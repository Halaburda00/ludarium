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


def test_a_missing_password_stops_the_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """No fallback, deliberately: the run command binds 0.0.0.0."""

    monkeypatch.delenv("LUDARIUM_PASSWORD")

    with pytest.raises(ConfigurationError, match="password"):
        get_settings()


def test_a_blank_password_is_not_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LUDARIUM_PASSWORD=` in a .env is how "unset" actually arrives."""

    monkeypatch.setenv("LUDARIUM_PASSWORD", "   ")

    with pytest.raises(ConfigurationError, match="must not be blank"):
        get_settings()


def test_the_password_is_masked_in_repr() -> None:
    settings = get_settings()

    rendered = f"{settings!r} {settings.model_dump()}"

    assert settings.password.get_secret_value() not in rendered


def test_a_blank_username_is_not_a_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """The twin of the password check, and the same `.env` mistake makes it."""

    monkeypatch.setenv("LUDARIUM_USERNAME", "  ")

    with pytest.raises(ConfigurationError, match="must not be blank"):
        get_settings()


IGDB_VARIABLES = ("LUDARIUM_IGDB_CLIENT_ID", "LUDARIUM_IGDB_CLIENT_SECRET")


def test_igdb_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """An instance with no Twitch application still starts; it enriches nothing."""

    for name in IGDB_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    settings = get_settings()

    assert settings.igdb_client_id is None
    assert settings.igdb_client_secret is None


def test_igdb_credentials_are_read_under_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUDARIUM_IGDB_CLIENT_ID", "not-a-real-client-id")
    monkeypatch.setenv("LUDARIUM_IGDB_CLIENT_SECRET", "not-a-real-secret")

    settings = get_settings()

    assert settings.igdb_client_id == "not-a-real-client-id"
    assert settings.igdb_client_secret is not None
    assert settings.igdb_client_secret.get_secret_value() == "not-a-real-secret"
    assert "not-a-real-secret" not in f"{settings!r} {settings.model_dump()}"


def test_blank_igdb_lines_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` ships both lines empty, which is how "unset" arrives."""

    monkeypatch.setenv("LUDARIUM_IGDB_CLIENT_ID", "")
    monkeypatch.setenv("LUDARIUM_IGDB_CLIENT_SECRET", "   ")

    settings = get_settings()

    assert settings.igdb_client_id is None
    assert settings.igdb_client_secret is None


@pytest.mark.parametrize("present", IGDB_VARIABLES)
def test_half_an_igdb_application_stops_the_instance(
    monkeypatch: pytest.MonkeyPatch, present: str
) -> None:
    """At start, where whoever set it is still looking, not at the first enrichment."""

    for name in IGDB_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(present, "not-a-real-value-for-this-half")

    with pytest.raises(ConfigurationError, match="set both LUDARIUM_IGDB_CLIENT_ID") as caught:
        get_settings()

    assert "not-a-real-value-for-this-half" not in str(caught.value)
