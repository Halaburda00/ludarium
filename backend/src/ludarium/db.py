"""The engine, the session factory, and what a transaction means on SQLite.

pysqlite opens no transaction for a `SELECT` — it manages transactions itself
and only for DML — so an endpoint answering with two queries has no snapshot
between them and a concurrent write lands in the gap (#32). Emitting `BEGIN`
from the `begin` event is what makes SQLAlchemy's boundaries real ones: pysqlite
skips its own implicit BEGIN when a transaction is already open, so ours is the
only one, and `SELECT` is inside it like everything else.

That is not free, and the shape of the cost is why the mode is chosen per
request rather than fixed. Measured on this schema, one writer against six
readers:

- A deferred `BEGIN` holds a shared lock for the life of the read. Under the
  rollback journal a commit behind a 200 ms read waited **230 ms**; under WAL
  the same commit took **1 ms**, because WAL readers and one writer do not
  exclude each other. WAL is not an optimisation here, it is what makes the
  snapshot affordable.
- A transaction that reads and then writes under a deferred `BEGIN` has to
  upgrade a shared lock it already holds, and two of those deadlock: SQLite
  answers `database is locked` immediately, without consulting `busy_timeout`,
  because waiting could not resolve it. Announcing the write up front with
  `BEGIN IMMEDIATE` serialises them instead, and both commit.

A deferred transaction is therefore held to reading by `PRAGMA query_only`. The
upgrade happens at the write statement and not at the commit, so a handler that
writes and never commits deadlocks just as well; nothing short of the database
refusing the statement catches that reliably.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Final

from fastapi import Depends, Request
from sqlalchemy import Connection, event, make_url
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

# The options the `begin` listener reads. A transaction is deferred and allowed
# to write unless a caller says otherwise, which keeps the plain factory usable
# for tooling and tests.
BEGIN_MODE: Final = "sqlite_begin"
READ_ONLY: Final = "sqlite_read_only"
DEFERRED: Final = "DEFERRED"
IMMEDIATE: Final = "IMMEDIATE"

# Methods that must not write, and so may hold nothing but a shared lock. The
# guard in `tests/test_read_endpoints.py` is what keeps this true, because a GET
# that writes would take the deferred path and reintroduce the deadlock.
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

# Long enough to outlast a sync's commit, short enough that a stuck request
# fails rather than hangs. Set explicitly rather than left to the driver's
# default, because the number matters and an implicit one cannot be read here.
BUSY_TIMEOUT_MS: Final = 5000


class Database:
    """Owns the engine and the session factories for one application instance."""

    def __init__(self, url: str) -> None:
        ensure_sqlite_directory(url)

        self.engine: AsyncEngine = create_async_engine(url)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine.sync_engine, "connect", _set_sqlite_pragmas)
            event.listen(self.engine.sync_engine, "begin", _begin)

        # General purpose: deferred, and allowed to write. Tests and tooling hold
        # a session open across other work, and one that took the write lock for
        # its lifetime would block every request beside it.
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        # The two a request gets. Same engine, same pool; only the announcement
        # differs.
        self.reading_session_factory = async_sessionmaker(
            self.engine.execution_options(**{READ_ONLY: True}),
            expire_on_commit=False,
        )
        self.writing_session_factory = async_sessionmaker(
            self.engine.execution_options(**{BEGIN_MODE: IMMEDIATE}),
            expire_on_commit=False,
        )

    async def dispose(self) -> None:
        await self.engine.dispose()


def _set_sqlite_pragmas(connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    cursor = connection.cursor()
    try:
        # Per connection, not per database: without this every ON DELETE policy
        # in docs/schema.md is decorative (ADR-0004).
        cursor.execute("PRAGMA foreign_keys = ON")
        # Persisted in the file header, so this is a no-op after the first
        # connection to an existing database. A `:memory:` database answers
        # "memory" and ignores it, which is why the test suite exercises the
        # BEGIN modes and a file-backed test covers the journal.
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def _begin(connection: Connection) -> None:
    """Announce what the transaction may do, and then hold SQLite to it.

    `query_only` is the enforcement rather than a second opinion: a deferred
    transaction that writes has to upgrade a lock it already holds, and that is
    the one contention SQLite refuses outright instead of waiting out. Refusing
    the write here turns a deadlock that needs two concurrent requests to appear
    into an error the first request raises on its own — and it catches the write
    itself, which is where the upgrade happens, rather than a commit that may
    never come.

    Set outside the transaction, where it can still take effect, and set on both
    branches because connections are pooled: one left read-only would refuse the
    next request's writes.
    """

    options = connection.get_execution_options()
    connection.exec_driver_sql(f"PRAGMA query_only = {'ON' if options.get(READ_ONLY) else 'OFF'}")
    connection.exec_driver_sql(f"BEGIN {options.get(BEGIN_MODE, DEFERRED)}")


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
    """One session per request. **The endpoint commits, not this.**

    Deliberate, and the opposite of it is worse: code after `yield` in a
    dependency runs once the response has gone out, so a commit here would tell
    the client the write succeeded and only then be free to fail. Committing in
    the endpoint turns the same failure into a 500 the caller can act on.

    The method chooses the transaction mode. It is the one thing known before
    the endpoint runs that says whether this request will write, and getting it
    from the request rather than from a per-endpoint marker means a new endpoint
    cannot forget to declare itself.
    """

    database: Database = request.app.state.database
    factory = (
        database.reading_session_factory
        if request.method in SAFE_METHODS
        else database.writing_session_factory
    )
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
