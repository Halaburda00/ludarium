import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    # The single account (ADR-0003). Required, with no fallback: a default that
    # works when unset is a backend on 0.0.0.0 with a password everyone knows.
    username: str = "ludarium"
    password: SecretStr
    database_url: str = "sqlite+aiosqlite:///./data/ludarium.db"
    # Where files the instance fetches live — cover art today. Beside the
    # default database, so the one directory to back up and to keep out of git
    # and the image stays one directory (ADR-0019).
    data_dir: Path = Path("./data")
    log_level: LogLevel = "INFO"
    # IGDB authenticates through a Twitch application (M2a). Optional: without
    # one the instance still syncs libraries and simply enriches nothing.
    igdb_client_id: str | None = None
    igdb_client_secret: SecretStr | None = None
    # RAWG, for Metacritic scores. Optional for the same reason, and a key the
    # user registers themselves: RAWG's free tier is 20 000 requests a month.
    rawg_api_key: SecretStr | None = None
    # How often each platform's accounts are synced without being asked, in
    # hours; 0 turns it off. One figure for every platform, and a JSON object
    # overriding it per platform: `LUDARIUM_SYNC_INTERVALS='{"epic": 24}'`.
    sync_interval_hours: int = Field(default=6, ge=0)
    sync_intervals: Annotated[dict[str, Annotated[int, Field(ge=0)]], NoDecode] = Field(
        default_factory=dict
    )
    # A made-up library on an empty database, shown read-only to anyone who
    # opens it, signed in without a password (`ludarium.demo`).
    demo: bool = False

    # `LUDARIUM_USERNAME=` and `LUDARIUM_PASSWORD=` in a .env are the way "unset"
    # actually reaches us, and pydantic would take the empty string for an answer.
    @field_validator("username")
    @classmethod
    def _reject_blank_username(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("password")
    @classmethod
    def _reject_blank_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    # Blank is unset here too, and for the same reason as above — but optional,
    # so it becomes None rather than an error.
    @field_validator("igdb_client_id", "igdb_client_secret", "rawg_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    # Decoded here rather than by pydantic-settings, so that blank is unset as
    # it is above, and a typo is reported as this field's problem instead of
    # a parser's traceback.
    @field_validator("sync_intervals", mode="before")
    @classmethod
    def _decode_intervals(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if not value.strip():
            return {}
        try:
            decoded: object = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError('must be a JSON object, such as {"epic": 24}') from exc
        return decoded

    @model_validator(mode="after")
    def _igdb_needs_both_halves(self) -> "Settings":
        # Half an application fails on the first enrichment, hours after start;
        # at start it fails where the person who set it is still looking.
        if (self.igdb_client_id is None) != (self.igdb_client_secret is None):
            raise ValueError(
                "set both LUDARIUM_IGDB_CLIENT_ID and LUDARIUM_IGDB_CLIENT_SECRET, or neither"
            )
        return self

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
