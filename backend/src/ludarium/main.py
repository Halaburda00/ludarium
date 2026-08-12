import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from ludarium import __version__
from ludarium.api import health
from ludarium.config import Settings, get_settings
from ludarium.db import Database
from ludarium.seed import seed_providers


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    database = Database(settings.database_url)
    app.state.database = database
    try:
        try:
            async with database.session_factory() as session:
                await seed_providers(session)
        except OperationalError as exc:
            raise RuntimeError(
                "the database has no schema yet — run `uv run alembic upgrade head`"
            ) from exc
        yield
    finally:
        # Also on a failed start: whatever went wrong, the pool it opened is ours
        # to close.
        await database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Ludarium", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    return app


app = create_app()
