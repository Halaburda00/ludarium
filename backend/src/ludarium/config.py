from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

FERNET_KEY_HINT = (
    'generate one with: uv run python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


class ConfigurationError(RuntimeError):
    """The environment does not describe a runnable instance."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LUDARIUM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    secret_key: SecretStr
    encryption_key: SecretStr
    database_url: str = "sqlite+aiosqlite:///./data/ludarium.db"
    log_level: LogLevel = "INFO"

    @field_validator("encryption_key")
    @classmethod
    def _reject_unusable_fernet_key(cls, value: SecretStr) -> SecretStr:
        # A key that only fails when the first token is written would surface as a
        # broken account connection hours later, so it is rejected at startup.
        try:
            Fernet(value.get_secret_value().encode())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"is not a valid Fernet key — {FERNET_KEY_HINT}") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Only the field name and the reason. Pydantic's own rendering — and the
        # chained traceback — repeat the raw input, which here is the environment
        # (rule 7). `from None` drops the chain for the same reason.
        problems = "\n".join(
            f"  {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigurationError(
            f"Ludarium cannot start, the environment is incomplete:\n{problems}"
        ) from None
