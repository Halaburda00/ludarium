import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

from cryptography.fernet import Fernet

# Set before anything imports ludarium.main, which builds the module-level app.
# Assignment rather than setdefault: a developer's real backend/.env is picked up
# from the working directory otherwise, and tests would use the real database.
TEST_SECRET_KEY = "test-secret-key"
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()
os.environ["LUDARIUM_SECRET_KEY"] = TEST_SECRET_KEY
os.environ["LUDARIUM_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
os.environ["LUDARIUM_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import create_engine, make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from ludarium.config import Settings  # noqa: E402
from ludarium.db import Database  # noqa: E402
from ludarium.enums import ProviderKind, SourceKind  # noqa: E402
from ludarium.main import create_app  # noqa: E402
from ludarium.models import AppUser, Base, Provider  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def sync_url(url: str) -> str:
    """The same SQLite file through pysqlite, for the sync-only tooling."""

    return make_url(url).set(drivername="sqlite").render_as_string(hide_password=False)


def create_schema(url: str) -> None:
    engine = create_engine(sync_url(url))
    Base.metadata.create_all(engine)
    engine.dispose()


def alembic_config(url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


async def make_user(session: AsyncSession, username: str = "owner") -> AppUser:
    user = AppUser(username=username, password_hash="not-a-hash")
    session.add(user)
    await session.flush()
    return user


async def make_provider(session: AsyncSession, key: str = "steam") -> Provider:
    provider = Provider(
        key=key,
        kind=ProviderKind.PLATFORM,
        source_kind=SourceKind.PLATFORM_API,
        display_name=key.title(),
    )
    session.add(provider)
    await session.flush()
    return provider


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        secret_key=SecretStr(TEST_SECRET_KEY),
        encryption_key=SecretStr(TEST_ENCRYPTION_KEY),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ludarium.db'}",
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    """A database with the schema in place, created from the models.

    Not from the migration, which would make every test wait on it; a drift test
    keeps the two equivalent.
    """

    create_schema(settings.database_url)
    database = Database(settings.database_url)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(db: Database) -> AsyncIterator[AsyncSession]:
    async with db.session_factory() as session:
        yield session


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    create_schema(settings.database_url)
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
