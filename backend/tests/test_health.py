from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from ludarium import __version__
from ludarium.db import get_session


def test_health_reports_the_version_and_a_reachable_database(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "database": True}


def test_health_is_unavailable_when_the_database_is_not(app: FastAPI) -> None:
    class UnreachableSession:
        async def execute(self, *args: Any, **kwargs: Any) -> None:
            raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    async def unreachable_session() -> AsyncIterator[UnreachableSession]:
        yield UnreachableSession()

    app.dependency_overrides[get_session] = unreachable_session

    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "version": __version__, "database": False}
