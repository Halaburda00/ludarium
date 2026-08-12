import pytest
from conftest import sync_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.config import Settings
from ludarium.db import Database
from ludarium.main import create_app
from ludarium.models import Provider


def test_startup_seeds_the_providers(client: TestClient, settings: Settings) -> None:
    client.get("/api/health")

    engine = create_engine(sync_url(settings.database_url))
    try:
        with engine.connect() as connection:
            keys = set(connection.scalars(select(Provider.key)))
    finally:
        engine.dispose()

    assert keys == {"steam", "manual"}


def test_startup_without_a_schema_says_what_to_run(settings: Settings) -> None:
    # The `app` fixture is not used here: the point is a database nobody migrated.
    with (
        pytest.raises(RuntimeError, match="alembic upgrade head"),
        TestClient(create_app(settings)),
    ):
        pass  # pragma: no cover


def test_a_failed_start_closes_the_pool(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the missing-schema case: any failure has to hand the engine back."""

    async def explode(session: AsyncSession) -> None:
        raise RuntimeError("the seed disagreed with the schema")

    disposed: list[Database] = []
    original = Database.dispose

    async def spy(self: Database) -> None:
        disposed.append(self)
        await original(self)

    monkeypatch.setattr("ludarium.main.seed_providers", explode)
    monkeypatch.setattr(Database, "dispose", spy)

    with (
        pytest.raises(RuntimeError, match="disagreed with the schema"),
        TestClient(create_app(settings)),
    ):
        pass  # pragma: no cover

    assert disposed
