import os
from collections.abc import Iterator
from pathlib import Path

from cryptography.fernet import Fernet

# Set before anything imports ludarium.main, which builds the module-level app.
# Assignment rather than setdefault: a developer's real backend/.env is picked up
# from the working directory otherwise, and tests would use the real database.
TEST_ENCRYPTION_KEY = Fernet.generate_key().decode()
os.environ["LUDARIUM_SECRET_KEY"] = "test-secret-key"
os.environ["LUDARIUM_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
os.environ["LUDARIUM_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from ludarium.config import Settings  # noqa: E402
from ludarium.main import create_app  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        secret_key=SecretStr("test-secret-key"),
        encryption_key=SecretStr(TEST_ENCRYPTION_KEY),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ludarium.db'}",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
