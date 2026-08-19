import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import sync_url
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from ludarium.config import Settings
from ludarium.db import (
    DEFERRED,
    IMMEDIATE,
    Database,
    SessionDep,
    ensure_sqlite_directory,
)
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

    # Not an equality check on the table: the bootstrapped account is in there too.
    assert "kept" in usernames
    assert "dropped" not in usernames


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


async def test_a_select_now_runs_inside_a_transaction(database: Database) -> None:
    """The whole of #32. pysqlite opens one for DML and not for a SELECT.

    Without this an endpoint answering with two queries has no snapshot between
    them, and `GET /api/works` had to drop rows that a concurrent removal had
    invalidated between the page query and the entitlement query.
    """

    async with database.session_factory() as session:
        await session.execute(text("SELECT 1"))
        raw = (await (await session.connection()).get_raw_connection()).driver_connection

        assert raw is not None
        assert raw.in_transaction is True


async def test_the_journal_is_wal(database: Database) -> None:
    """Not a tuning choice: it is what makes holding a read snapshot affordable.

    Under the rollback journal a shared lock held for the life of a read blocks
    the writer behind it — measured at 230 ms for a commit behind a 200 ms read,
    against 1 ms under WAL.
    """

    async with database.session_factory() as session:
        assert (await session.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"


async def test_a_read_holds_its_snapshot_across_a_committed_write(database: Database) -> None:
    async with database.writing_session_factory() as setup:
        await setup.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        await setup.execute(text("INSERT INTO t (id, v) VALUES (1, 'before')"))
        await setup.commit()

    async with database.session_factory() as reader:
        first = (await reader.execute(text("SELECT v FROM t WHERE id = 1"))).scalar_one()
        async with database.writing_session_factory() as writer:
            await writer.execute(text("UPDATE t SET v = 'after' WHERE id = 1"))
            await writer.commit()
        second = (await reader.execute(text("SELECT v FROM t WHERE id = 1"))).scalar_one()

    # The point of the change: two queries in one request are one view of the
    # database, whatever lands in between.
    assert (first, second) == ("before", "before")


async def test_two_transactions_that_read_then_write_both_commit(database: Database) -> None:
    """A deferred BEGIN cannot survive this, which is why writes announce themselves.

    Both hold a shared lock from their SELECT and both need to upgrade it.
    SQLite answers the second one `database is locked` at once — `busy_timeout`
    is not consulted, because waiting could not resolve a deadlock. `BEGIN
    IMMEDIATE` takes the write lock up front, so the two serialise instead.
    """

    async with database.writing_session_factory() as setup:
        await setup.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        await setup.execute(text("INSERT INTO t (id, v) VALUES (1, 'a')"))
        await setup.commit()

    async def read_then_write(value: str) -> None:
        async with database.writing_session_factory() as session:
            await session.execute(text("SELECT COUNT(*) FROM t"))
            await asyncio.sleep(0.05)
            await session.execute(text("UPDATE t SET v = :v WHERE id = 1"), {"v": value})
            await session.commit()

    await asyncio.gather(read_then_write("A"), read_then_write("B"))


async def test_a_deferred_pair_is_what_the_immediate_one_is_avoiding(database: Database) -> None:
    """The test above passes just as well if the modes are never applied."""

    async with database.writing_session_factory() as setup:
        await setup.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        await setup.execute(text("INSERT INTO t (id, v) VALUES (1, 'a')"))
        await setup.commit()

    async def read_then_write(value: str) -> None:
        async with database.session_factory() as session:
            await session.execute(text("SELECT COUNT(*) FROM t"))
            await asyncio.sleep(0.05)
            await session.execute(text("UPDATE t SET v = :v WHERE id = 1"), {"v": value})
            await session.commit()

    with pytest.raises(OperationalError, match="database is locked"):
        await asyncio.gather(read_then_write("A"), read_then_write("B"))


def test_the_method_chooses_the_transaction_mode(app: FastAPI) -> None:
    """A GET may hold nothing but a shared lock; anything else announces its write.

    Asserted on the statement that reaches the driver rather than on the
    execution option that produced it: the option is the intent and `BEGIN
    IMMEDIATE` is the thing SQLite acts on.
    """

    begins: list[str] = []

    @app.get("/test/reading")
    async def reading(session: SessionDep) -> None:
        await session.execute(text("SELECT 1"))

    @app.post("/test/writing")
    async def writing(session: SessionDep) -> None:
        await session.execute(text("SELECT 1"))

    with TestClient(app) as client:
        engine = client.app.state.database.engine.sync_engine  # type: ignore[attr-defined]

        def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
            if statement.startswith("BEGIN"):
                begins.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            client.get("/test/reading")
            client.post("/test/writing")
        finally:
            event.remove(engine, "before_cursor_execute", record)

    assert begins == [f"BEGIN {DEFERRED}", f"BEGIN {IMMEDIATE}"]
