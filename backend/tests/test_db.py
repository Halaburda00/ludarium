from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import sync_url
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from ludarium.config import Settings
from ludarium.db import Database, SessionDep, ensure_sqlite_directory
from ludarium.models import AppUser


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings.database_url)
    try:
        yield db
    finally:
        await db.dispose()


async def test_foreign_keys_pragma_is_on_for_every_connection(database: Database) -> None:
    for _ in range(2):
        async with database.session_factory() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        # Return the connection and take a fresh one: the pragma is per connection.
        await database.engine.dispose()


async def test_foreign_keys_are_actually_enforced(database: Database) -> None:
    async with database.session_factory() as session:
        await session.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
        await session.execute(
            text(
                "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER "
                "REFERENCES parent(id))"
            )
        )

        with pytest.raises(IntegrityError):
            await session.execute(text("INSERT INTO child (parent_id) VALUES (99)"))


async def test_in_memory_url_needs_no_directory() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    try:
        async with database.session_factory() as session:
            assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
    finally:
        await database.dispose()


def test_the_endpoint_commits_not_the_dependency(app: FastAPI, settings: Settings) -> None:
    """Pins the contract in `get_session`, in both directions.

    Auto-committing on teardown would be the tempting change, and it would put
    the commit after the response has already gone out.
    """

    @app.post("/test/without-a-commit")
    async def without_a_commit(session: SessionDep) -> None:
        session.add(AppUser(username="dropped", password_hash="not-a-hash"))
        await session.flush()

    @app.post("/test/with-a-commit")
    async def with_a_commit(session: SessionDep) -> None:
        session.add(AppUser(username="kept", password_hash="not-a-hash"))
        await session.commit()

    with TestClient(app) as client:
        client.post("/test/without-a-commit")
        client.post("/test/with-a-commit")

    engine = create_engine(sync_url(settings.database_url))
    try:
        with engine.connect() as connection:
            usernames = set(connection.scalars(select(AppUser.username)))
    finally:
        engine.dispose()

    assert usernames == {"kept"}


def test_a_postgres_url_creates_no_directory(tmp_path: Path) -> None:
    ensure_sqlite_directory(f"postgresql+asyncpg://user@host/{tmp_path.name}")

    assert list(tmp_path.iterdir()) == []


async def test_sqlite_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "ludarium.db"

    database = Database(f"sqlite+aiosqlite:///{target}")
    try:
        assert target.parent.is_dir()
    finally:
        await database.dispose()
