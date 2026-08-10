import pytest
from conftest import sync_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from ludarium.config import Settings
from ludarium.main import create_app
from ludarium.models import Provider


def test_startup_seeds_the_providers(client: TestClient, settings: Settings) -> None:
    client.get("/api/health")

    engine = create_engine(sync_url(settings.database_url))
    try:
        with engine.connect() as connection:
            keys = set(connection.scalars(select(Provider.key)))
    finally:
        engine.dispose()

    assert keys == {"steam", "manual"}


def test_startup_without_a_schema_says_what_to_run(settings: Settings) -> None:
    # The `app` fixture is not used here: the point is a database nobody migrated.
    with (
        pytest.raises(RuntimeError, match="alembic upgrade head"),
        TestClient(create_app(settings)),
    ):
        pass  # pragma: no cover
