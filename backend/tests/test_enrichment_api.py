import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_matching import Igdb
from test_metacritic import Rawg

from ludarium import steps as steps_module
from ludarium.enrichment import EnrichmentRun
from ludarium.enums import ItemKind, SyncStatus, SyncTrigger
from ludarium.models import Provider, SyncRun, Work
from ludarium.providers import igdb as igdb_module
from ludarium.providers import rawg as rawg_module
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
    for module in (steam_module, store_module, igdb_module):
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


async def runs_of(session: AsyncSession, key: str) -> list[SyncRun]:
    return list(
        await session.scalars(
            select(SyncRun)
            .join(Provider, Provider.id == SyncRun.provider_id)
            .where(Provider.key == key)
            .order_by(SyncRun.id)
        )
    )


async def store_runs(session: AsyncSession) -> list[SyncRun]:
    return await runs_of(session, "steam_store")


def with_igdb(client: TestClient) -> None:
    """The app as an instance with an IGDB application configured."""

    settings = client.app.state.settings  # type: ignore[attr-defined]
    client.app.state.settings = settings.model_copy(  # type: ignore[attr-defined]
        update={
            "igdb_client_id": "not-a-real-client-id",
            "igdb_client_secret": SecretStr("not-a-real-secret"),
        }
    )


def with_rawg(client: TestClient) -> None:
    settings = client.app.state.settings  # type: ignore[attr-defined]
    client.app.state.settings = settings.model_copy(  # type: ignore[attr-defined]
        update={"rawg_api_key": SecretStr("not-a-real-rawg-key")}
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
    # One request for what each app is, one for its reviews: two resources,
    # cached apart, because they go stale at different rates.
    assert asked.call_count == 2
    (run,) = await store_runs(session)
    assert (run.status, run.account_id, run.trigger) == (SyncStatus.SUCCESS, None, "manual")
    assert await kinds(session) == [ItemKind.GAME] * 3


@respx.mock
async def test_a_sync_that_stored_nothing_triggers_nothing(
    connected: TestClient, session: AsyncSession
) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(503))
    asked = respx.get(GET_ITEMS_URL).mock(return_value=store())

    (run,) = connected.post("/api/sync/steam").json()["runs"]

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

    (synced,) = response.json()["runs"]
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
    # Three apps asked about twice: once for their kind, once for their reviews.
    assert body["items_seen"] == 6
    assert await kinds(session) == [ItemKind.GAME] * 3


async def test_a_run_already_open_is_a_409(connected: TestClient, session: AsyncSession) -> None:
    provider = await session.scalar(select(Provider).where(Provider.key == "steam_store"))
    assert provider is not None
    session.add(SyncRun(provider_id=provider.id, account_id=None, trigger=SyncTrigger.MANUAL))
    await session.commit()

    response = connected.post("/api/enrichment/steam_store")

    assert response.status_code == 409
    assert "already enriching" in response.json()["detail"]


@respx.mock
async def test_with_igdb_configured_a_sync_is_classified_and_then_matched(
    connected: TestClient, session: AsyncSession
) -> None:
    with_igdb(connected)
    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    respx.get(GET_ITEMS_URL).mock(return_value=store())
    Igdb(
        [{"game": 1942, "uid": "292030"}, {"game": 72, "uid": "620"}],
        {1942: "The Witcher 3: Wild Hunt", 72: "Portal 2"},
        {1942: "co1wyy"},
    ).mount()

    assert connected.post("/api/sync/steam").status_code == 200

    (classified,) = await store_runs(session)
    (matched,) = await runs_of(session, "igdb")
    # After, not beside: matching takes only what classification called a game.
    assert classified.finished_at is not None and matched.started_at >= classified.finished_at
    assert matched.status is SyncStatus.SUCCESS
    anchors = await session.execute(select(Work.title, Work.igdb_id).order_by(Work.id))
    assert anchors.tuples().all() == [
        ("The Witcher 3: Wild Hunt", 1942),
        ("Portal 2", 72),
        ("Dota 2", None),
    ]
    # Covers in the same run, for what anchoring matched.
    data_dir = connected.app.state.settings.data_dir  # type: ignore[attr-defined]
    assert sorted(path.name for path in (data_dir / "covers" / "igdb").iterdir()) == [
        "co1wyy.jpg",
        "co1wyy_2x.jpg",
    ]


@respx.mock
async def test_without_an_igdb_application_matching_is_skipped_rather_than_failed(
    connected: TestClient, session: AsyncSession
) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    respx.get(GET_ITEMS_URL).mock(return_value=store())

    assert connected.post("/api/sync/steam").status_code == 200

    assert len(await store_runs(session)) == 1
    assert await runs_of(session, "igdb") == []
    igdb = await session.scalar(select(Provider.status).where(Provider.key == "igdb"))
    assert igdb is not SyncStatus.FAILED


def test_matching_by_hand_without_an_igdb_application_is_a_400(connected: TestClient) -> None:
    response = connected.post("/api/enrichment/igdb")

    assert response.status_code == 400
    assert "not configured" in response.json()["detail"]


def test_a_provider_with_no_step_is_a_400(connected: TestClient) -> None:
    response = connected.post("/api/enrichment/steam")

    assert response.status_code == 400
    assert "no enrichment step" in response.json()["detail"]


def test_an_unknown_provider_is_a_404(connected: TestClient) -> None:
    assert connected.post("/api/enrichment/xbox").status_code == 404


def test_enrichment_needs_a_session(client: TestClient) -> None:
    assert client.post("/api/enrichment/steam_store").status_code == 401


@respx.mock
async def test_with_rawg_configured_scores_follow_matching(
    connected: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rawg_module, "RETRY_BACKOFF_SECONDS", 0.0)
    with_igdb(connected)
    with_rawg(connected)
    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    respx.get(GET_ITEMS_URL).mock(return_value=store())
    Igdb([{"game": 72, "uid": "620"}], {72: "Portal 2"}).mount()
    Rawg(
        {"Portal 2": [4200]},
        {4200: ["https://store.steampowered.com/app/620/Portal_2/"]},
        {4200: {"slug": "portal-2", "metacritic": 95}},
    ).mount()

    assert connected.post("/api/sync/steam").status_code == 200

    (matched,) = await runs_of(session, "igdb")
    (scored,) = await runs_of(session, "rawg")
    # After: RAWG is asked only about what matching anchored.
    assert matched.finished_at is not None and scored.started_at >= matched.finished_at
    assert scored.status is SyncStatus.SUCCESS
    portal = await session.scalar(select(Work.metacritic_score).where(Work.igdb_id == 72))
    assert portal == 95


def test_scoring_by_hand_without_a_rawg_key_is_a_400(connected: TestClient) -> None:
    response = connected.post("/api/enrichment/rawg")

    assert response.status_code == 400
    assert "not configured" in response.json()["detail"]


@respx.mock
async def test_the_sync_names_the_steps_it_queued_and_only_those_set_up(
    connected: TestClient,
) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=library())
    respx.get(GET_ITEMS_URL).mock(return_value=store())

    # No IGDB application and no RAWG key here: neither step opens a run, so
    # naming them would have a client wait for ever.
    assert connected.post("/api/sync/steam").json()["enriching"] == ["steam_store"]


@respx.mock
async def test_a_partial_sync_is_followed_by_classifying_what_it_kept(
    connected: TestClient,
) -> None:
    """What a partial run keeps is new works like any other, and they need a kind."""

    body = recorded("steam/owned_games.json")
    body["response"]["games"].append({"appid": 4000})
    body["response"]["game_count"] += 1
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(200, json=body))
    respx.get(GET_ITEMS_URL).mock(return_value=store())

    response = connected.post("/api/sync/steam").json()

    assert response["runs"][0]["status"] == SyncStatus.PARTIAL
    assert response["enriching"] == ["steam_store"]


@respx.mock
async def test_a_failed_sync_queues_nothing(connected: TestClient) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(503))

    assert connected.post("/api/sync/steam").json()["enriching"] == []


@respx.mock
async def test_a_queued_step_reads_as_in_progress_until_it_ends(
    connected: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued before the answer, so no moment between the answer and the step reads as done."""

    seen: list[list[str]] = []
    scheduled = connected.app.state.scheduled  # type: ignore[attr-defined]

    async def watched(run: EnrichmentRun) -> None:
        # What the overview serves, read where the step can reach it: the test
        # client cannot be called from inside the loop the step runs on.
        seen.append(list(scheduled))

    monkeypatch.setitem(steps_module.STEPS, "steam_store", lambda context: watched)
    respx.get(OWNED_GAMES_URL).mock(return_value=library())

    assert connected.post("/api/sync/steam").status_code == 200

    assert seen == [["steam_store"]]
    assert connected.get("/api/sync/runs").json()["enriching"] == []


def test_the_overview_serves_what_is_queued(connected: TestClient) -> None:
    connected.app.state.scheduled.add(["steam_store", "igdb"])  # type: ignore[attr-defined]

    assert connected.get("/api/sync/runs").json()["enriching"] == ["steam_store", "igdb"]
