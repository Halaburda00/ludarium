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

from ludarium.enums import ItemKind, SyncStatus, SyncTrigger
from ludarium.models import Provider, SyncRun, Work
from ludarium.providers import steam as steam_module
from ludarium.providers import steam_store as store_module

FIXTURES = Path(__file__).parent / "fixtures"
OWNED_GAMES_URL = f"{steam_module.STEAM_API}{steam_module.OWNED_GAMES}"
GET_ITEMS_URL = f"{store_module.STORE_API}{store_module.GET_ITEMS}"


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def library() -> httpx.Response:
    return httpx.Response(200, json=recorded("steam/owned_games.json"))


def store() -> httpx.Response:
    """What the store said, live, about the three apps `owned_games.json` holds: all games."""

    return httpx.Response(200, json=recorded("steam_store/owned_games_items.json"))


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (steam_module, store_module):
        monkeypatch.setattr(module, "RETRY_BACKOFF_SECONDS", 0.0)
        monkeypatch.setattr(module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
def connected(client: TestClient) -> TestClient:
    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    with respx.mock:
        respx.get(OWNED_GAMES_URL).mock(return_value=library())
        account = {
            "provider": "steam",
            "external_account_id": "76561197960287930",
            "label": "Main",
            "credentials": "0123456789ABCDEF-not-a-real-key",
        }
        assert client.post("/api/accounts", json=account).status_code == 201
    return client


async def store_runs(session: AsyncSession) -> list[SyncRun]:
    return list(
        await session.scalars(
            select(SyncRun)
            .join(Provider, Provider.id == SyncRun.provider_id)
            .where(Provider.key == "steam_store")
            .order_by(SyncRun.id)
        )
    )


async def kinds(session: AsyncSession) -> list[ItemKind | None]:
    return list(await session.scalars(select(Work.item_kind).order_by(Work.id)))


@respx.mock
async def test_a_sync_is_followed_by_classifying_what_it_stored(
    connected: TestClient, session: AsyncSession
) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    asked = respx.get(GET_ITEMS_URL).mock(return_value=store())

    response = connected.post("/api/sync/steam")

    assert response.status_code == 200
    assert asked.call_count == 1
    (run,) = await store_runs(session)
    assert (run.status, run.account_id, run.trigger) == (SyncStatus.SUCCESS, None, "manual")
    assert await kinds(session) == [ItemKind.GAME] * 3


@respx.mock
async def test_a_sync_that_stored_nothing_triggers_nothing(
    connected: TestClient, session: AsyncSession
) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(503))
    asked = respx.get(GET_ITEMS_URL).mock(return_value=store())

    (run,) = connected.post("/api/sync/steam").json()

    assert run["status"] == SyncStatus.FAILED
    assert not asked.called
    assert await store_runs(session) == []


@respx.mock
async def test_a_store_outage_does_not_fail_the_sync_it_follows(
    connected: TestClient, session: AsyncSession
) -> None:
    """Rule 4 between two providers of one platform: the library landed, the store did not."""

    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(503))

    response = connected.post("/api/sync/steam")

    (synced,) = response.json()
    assert synced["status"] == SyncStatus.SUCCESS
    (run,) = await store_runs(session)
    assert run.status is SyncStatus.FAILED
    health = dict((await session.execute(select(Provider.key, Provider.status))).tuples().all())
    assert (health["steam"], health["steam_store"]) == (SyncStatus.SUCCESS, SyncStatus.FAILED)
    assert await kinds(session) == [None] * 3


@respx.mock
async def test_a_sync_while_the_store_is_still_classifying_skips_it(
    connected: TestClient, session: AsyncSession
) -> None:
    """Refused, not queued: the open run or the next one asks about what this sync added."""

    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    asked = respx.get(GET_ITEMS_URL).mock(return_value=store())
    provider = await session.scalar(select(Provider).where(Provider.key == "steam_store"))
    assert provider is not None
    session.add(SyncRun(provider_id=provider.id, account_id=None, trigger=SyncTrigger.MANUAL))
    await session.commit()

    response = connected.post("/api/sync/steam")

    assert response.status_code == 200
    assert not asked.called
    assert len(await store_runs(session)) == 1


@respx.mock
async def test_a_failed_classification_can_be_retried_by_hand(
    connected: TestClient, session: AsyncSession
) -> None:
    """The retry ADR-0019 names: the store came back, and the library need not sync again."""

    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    outage = respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(503))
    connected.post("/api/sync/steam")
    outage.mock(return_value=store())

    response = connected.post("/api/enrichment/steam_store")

    assert response.status_code == 200
    body = response.json()
    assert (body["provider"], body["status"], body["account_id"]) == (
        "steam_store",
        SyncStatus.SUCCESS,
        None,
    )
    assert body["items_seen"] == 3
    assert await kinds(session) == [ItemKind.GAME] * 3


async def test_a_run_already_open_is_a_409(connected: TestClient, session: AsyncSession) -> None:
    provider = await session.scalar(select(Provider).where(Provider.key == "steam_store"))
    assert provider is not None
    session.add(SyncRun(provider_id=provider.id, account_id=None, trigger=SyncTrigger.MANUAL))
    await session.commit()

    response = connected.post("/api/enrichment/steam_store")

    assert response.status_code == 409
    assert "already enriching" in response.json()["detail"]


def test_a_provider_with_no_step_is_a_400(connected: TestClient) -> None:
    response = connected.post("/api/enrichment/steam")

    assert response.status_code == 400
    assert "no enrichment step" in response.json()["detail"]


def test_an_unknown_provider_is_a_404(connected: TestClient) -> None:
    assert connected.post("/api/enrichment/epic").status_code == 404


def test_enrichment_needs_a_session(client: TestClient) -> None:
    assert client.post("/api/enrichment/steam_store").status_code == 401
