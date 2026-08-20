import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy.exc import OperationalError

from ludarium import __version__
from ludarium.api import accounts, auth, health, works
from ludarium.api import sync as sync_api
from ludarium.auth import bootstrap_user, current_session
from ludarium.config import Settings, get_settings
from ludarium.db import Database
from ludarium.seed import seed_providers

# Bounded on purpose, and not only for the user waiting on onboarding: a run
# that never returns is a `sync_run` row stuck at `running`, and the reclaim
# that unsticks it waits an hour (`sync.ORPHAN_AFTER`).
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=5.0)

# httpx logs every request at INFO with the full URL, and Steam's Web API key
# travels in the query string — so the stock configuration writes the user's own
# credential into the container log on every sync (rule 7).
#
# Silenced at these loggers rather than by raising the root level, so that
# turning `LUDARIUM_LOG_LEVEL` up to DEBUG to investigate a failing sync does not
# start leaking the key. That is precisely when someone would turn it up, and
# precisely when they would not be watching for this.
SILENCED = ("httpx", "httpcore")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    database = Database(settings.database_url)
    app.state.database = database
    # One client for the whole process: every provider shares the connection
    # pool, which is what `SteamProvider` means by taking one rather than
    # opening its own.
    app.state.http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    try:
        try:
            # Writing, so it announces itself: startup is not concurrent today,
            # and a second instance pointed at the same file would be.
            async with database.writing_session_factory() as session:
                await seed_providers(session)
                await bootstrap_user(
                    session,
                    username=settings.username,
                    password=settings.password.get_secret_value(),
                )
        except OperationalError as exc:
            # Only the one it can actually diagnose. `BEGIN IMMEDIATE` can also
            # fail here with `database is locked` — a second instance on the
            # same file, or a stale writer — and telling that operator to run a
            # migration sends them away from the real cause.
            if "no such table" not in str(exc.orig):
                raise
            raise RuntimeError(
                "the database has no schema yet — run `uv run alembic upgrade head`"
            ) from exc
        yield
    finally:
        # Also on a failed start: whatever went wrong, the pools it opened are
        # ours to close.
        await app.state.http.aclose()
        await database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # force, because basicConfig is a no-op once the root logger has a handler:
    # the module-level app below installs one at import time, and every later
    # create_app would silently ignore LUDARIUM_LOG_LEVEL.
    logging.basicConfig(level=settings.log_level, force=True)
    for name in SILENCED:
        logging.getLogger(name).setLevel(logging.WARNING)

    app = FastAPI(title="Ludarium", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    # A backstop, not the mechanism. Every endpoint on these routers already
    # takes `CurrentSession`, because it needs the user to scope by (ADR-0003);
    # this closes the one that some day will not. What actually enforces it is
    # `test_every_endpoint_but_the_public_ones_needs_a_session`, which asks the
    # running app rather than trusting either of the two.
    guarded = [Depends(current_session)]
    app.include_router(accounts.router, prefix="/api", dependencies=guarded)
    app.include_router(sync_api.router, prefix="/api", dependencies=guarded)
    app.include_router(works.router, prefix="/api", dependencies=guarded)
    return app


app = create_app()
