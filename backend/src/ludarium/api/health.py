import logging
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ludarium import __version__
from ludarium.db import SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: bool


@router.get("/health")
async def health(session: SessionDep, response: Response) -> HealthResponse:
    database_reachable = True
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("health check could not reach the database")
        database_reachable = False
        # A container healthcheck reads the status code, not the body.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if database_reachable else "degraded",
        version=__version__,
        database=database_reachable,
    )
