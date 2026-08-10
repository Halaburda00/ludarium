import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ludarium import __version__
from ludarium.api import health
from ludarium.config import Settings, get_settings
from ludarium.db import Database


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.database = Database(settings.database_url)
    try:
        yield
    finally:
        await app.state.database.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Ludarium", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    return app


app = create_app()
