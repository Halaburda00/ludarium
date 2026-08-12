from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import event, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry


class Database:
    """Owns the engine and the session factory for one application instance."""

    def __init__(self, url: str) -> None:
        ensure_sqlite_directory(url)

        self.engine: AsyncEngine = create_async_engine(url)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine.sync_engine, "connect", _set_sqlite_pragmas)

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()


def _set_sqlite_pragmas(connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    # Per connection, not per database: without this every ON DELETE policy in
    # docs/schema.md is decorative (ADR-0004).
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


def ensure_sqlite_directory(url: str) -> None:
    """Create the parent directory so SQLite can create the file on first connect.

    Alembic needs this as much as the application does: `alembic upgrade head` is
    the documented first command on a fresh checkout, where `data/` does not exist
    yet and SQLite would only answer `unable to open database file`.
    """

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return
    database = parsed.database
    if database is None or database == ":memory:":
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database: Database = request.app.state.database
    async with database.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
