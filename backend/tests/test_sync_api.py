import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import SyncStatus, SyncTrigger
from ludarium.models import Account, AppUser, Entitlement, SyncRun
from ludarium.providers import steam as steam_module

FIXTURES = Path(__file__).parent / "fixtures" / "steam"
OWNED_GAMES_URL = f"{steam_module.STEAM_API}{steam_module.OWNED_GAMES}"
API_KEY = "0123456789ABCDEF-not-a-real-key"
STEAM_ID = "76561197960287930"


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retries are the subject of the provider's own suite, the waiting is nobody's."""

    monkeypatch.setattr(steam_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(steam_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
def connected(client: TestClient) -> TestClient:
    """Signed in with one Steam account already connected, the way onboarding leaves it."""

    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    with respx.mock:
        respx.get(OWNED_GAMES_URL).mock(
            return_value=httpx.Response(200, json=recorded("owned_games.json"))
        )
        assert (
            client.post(
                "/api/accounts",
                json={
                    "provider": "steam",
                    "external_account_id": STEAM_ID,
                    "label": "Main",
                    "credentials": API_KEY,
                },
            ).status_code
            == 201
        )
    return client


@respx.mock
def test_a_sync_returns_the_run_counters(connected: TestClient) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )

    response = connected.post("/api/sync/steam")

    assert response.status_code == 200
    (run,) = response.json()
    assert run["provider"] == "steam"
    assert run["status"] == SyncStatus.SUCCESS
    assert (run["items_seen"], run["items_added"], run["items_removed"]) == (3, 3, 0)
    assert run["finished_at"] is not None


@respx.mock
async def test_a_failure_partway_is_a_status_not_a_500(
    connected: TestClient, session: AsyncSession
) -> None:
    """Rule 4 seen from the API: the caller gets a run that failed, not an error page.

    And rule 1 holds through it — the library the previous run landed is still
    there, because a failed run rolls back and marks nothing removed.
    """

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )
    assert connected.post("/api/sync/steam").status_code == 200
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(503))

    response = connected.post("/api/sync/steam")

    assert response.status_code == 200
    (run,) = response.json()
    assert run["status"] == SyncStatus.FAILED
    assert run["items_removed"] == 0
    assert run["error_text"]
    live = await session.scalars(select(Entitlement).where(Entitlement.removed_at.is_(None)))
    assert len(list(live)) == 3


async def test_a_sync_already_in_flight_is_a_409(
    connected: TestClient, session: AsyncSession
) -> None:
    """The double-click. One open run per account is the index; this is its status code."""

    account = await session.scalar(select(Account))
    assert account is not None
    session.add(
        SyncRun(
            provider_id=account.provider_id,
            account_id=account.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
        )
    )
    await session.commit()

    response = connected.post("/api/sync/steam")

    assert response.status_code == 409
    assert "already syncing" in response.json()["detail"]


def test_syncing_an_unknown_provider_is_a_404(connected: TestClient) -> None:
    assert connected.post("/api/sync/epic").status_code == 404


def test_syncing_a_provider_with_no_account_is_a_404(client: TestClient) -> None:
    client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})

    response = client.post("/api/sync/steam")

    assert response.status_code == 404
    assert "connected" in response.json()["detail"]


async def test_a_key_the_current_encryption_key_cannot_read_says_which_key(
    connected: TestClient, session: AsyncSession
) -> None:
    """`LUDARIUM_ENCRYPTION_KEY` changed, which is the only way this happens.

    The message names the setting and never the ciphertext (rule 7) — the whole
    point of `CredentialDecryptionError` carrying no payload.
    """

    account = await session.scalar(select(Account))
    assert account is not None
    account.credentials_encrypted = b"not-a-fernet-token"
    await session.commit()

    response = connected.post("/api/sync/steam")

    assert response.status_code == 500
    assert "LUDARIUM_ENCRYPTION_KEY" in response.json()["detail"]
    assert "not-a-fernet-token" not in response.text


@respx.mock
def test_the_overview_carries_both_the_providers_and_their_runs(connected: TestClient) -> None:
    """One call, because a status panel cannot say anything useful with half of it."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )
    connected.post("/api/sync/steam")

    body = connected.get("/api/sync/runs").json()

    steam = next(provider for provider in body["providers"] if provider["key"] == "steam")
    assert steam["status"] == SyncStatus.SUCCESS
    assert steam["last_success_at"] is not None
    assert steam["last_error"] is None
    assert [run["provider"] for run in body["runs"]] == ["steam"]


def test_syncing_needs_a_session(client: TestClient) -> None:
    assert client.post("/api/sync/steam").status_code == 401
    assert client.get("/api/sync/runs").status_code == 401


async def test_an_account_with_no_stored_key_cannot_be_synced(
    connected: TestClient, session: AsyncSession
) -> None:
    """A derived account is the real case — discovered inside an import, never connected.

    This one is that shape reached the short way, since the importer that makes
    them is M4.
    """

    account = await session.scalar(select(Account))
    assert account is not None
    account.credentials_encrypted = None
    await session.commit()

    response = connected.post("/api/sync/steam")

    assert response.status_code == 400
    assert "no stored credentials" in response.json()["detail"]


def test_a_provider_with_no_library_client_cannot_be_synced(connected: TestClient) -> None:
    """`manual` owns entitlements and has nothing to ask, which is not an outage.

    Answered before any account is looked at, let alone any credential
    decrypted: it is a fact about the provider, not about what is stored under
    it.
    """

    response = connected.post("/api/sync/manual")

    assert response.status_code == 400
    assert "library client" in response.json()["detail"]


async def test_another_users_account_is_not_synced_along(
    connected: TestClient, session: AsyncSession
) -> None:
    """ADR-0003: `user_id` is real and populated, and the UI being single-tenant is not a filter.

    The ADR is explicit that the discipline only holds if every query remembers
    the column, and a single-user fixture can never notice a query that forgot.
    So there are two users here, which is the only way to tell the difference.
    """

    stranger = AppUser(username="somebody-else", password_hash="not-a-hash")
    session.add(stranger)
    await session.flush()
    mine = await session.scalar(select(Account))
    assert mine is not None
    theirs = Account(
        user_id=stranger.id,
        provider_id=mine.provider_id,
        external_account_id="76561197960287999",
        label="Not mine",
        credentials_encrypted=b"unused",
    )
    session.add(theirs)
    await session.commit()

    with respx.mock:
        respx.get(OWNED_GAMES_URL).mock(
            return_value=httpx.Response(200, json=recorded("owned_games.json"))
        )
        response = connected.post("/api/sync/steam")

    assert response.status_code == 200
    # Only mine. Theirs would have failed on `b"unused"` long before that.
    assert [run["account_id"] for run in response.json()] == [mine.id]
