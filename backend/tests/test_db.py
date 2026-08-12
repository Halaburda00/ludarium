from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ludarium.config import Settings
from ludarium.db import Database, ensure_sqlite_directory


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
