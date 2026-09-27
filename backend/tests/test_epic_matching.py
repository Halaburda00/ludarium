import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_matching import Igdb, anchor, own, read, work_of

from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import ItemKind, MatchAction, MatchLayer, SyncStatus
from ludarium.matching import anchor_epic_works
from ludarium.models import Account, Entitlement, EntitlementWork, ExternalId, MatchAudit, Work
from ludarium.providers import IgdbClient, IgdbCredentials, MemoryTokenStore, RequestLimiter
from ludarium.providers import epic as epic_module
from ludarium.providers.epic import EpicCatalog
from ludarium.seed import seed_providers

FIXTURES = Path(__file__).parent / "fixtures" / "epic"
GONE_HOME_NS = "52326e805bac4619a4a8fac165363a42"
GONE_HOME_OFFER = "56b3f851cc664490a9911040da23a611"
KF2_NS = "affc33b82405457595a032f00284abd2"
EPIC = 26
APP_TOKEN = {"access_token": "eg1~not-a-real-app-token", "expires_in": 14400}


def recorded_offers(namespace: str) -> Any:
    return json.loads((FIXTURES / f"offers_{namespace}.json").read_text())


def mount_catalog(offers: dict[str, list[dict[str, Any]]] | None = None) -> respx.Route:
    """The catalogue's offers, by namespace: recorded ones unless a test says otherwise."""

    def answer(request: httpx.Request) -> httpx.Response:
        namespace = request.url.path.split("/namespace/")[1].split("/")[0]
        if offers is not None and namespace in offers:
            elements = offers[namespace]
        elif (FIXTURES / f"offers_{namespace}.json").exists():
            elements = recorded_offers(namespace)["elements"]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"elements": elements, "paging": {"total": len(elements)}})

    respx.post(epic_module.OAUTH).mock(return_value=httpx.Response(200, json=APP_TOKEN))
    return respx.get(url__startswith=f"{epic_module.CATALOG}/namespace/").mock(side_effect=answer)


@pytest.fixture(autouse=True)
def instant_epic_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epic_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(epic_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def igdb() -> AsyncIterator[IgdbClient]:
    async with httpx.AsyncClient() as http:
        yield IgdbClient(
            IgdbCredentials(client_id="not-a-real-client-id", client_secret="not-a-real-secret"),
            http,
            tokens=MemoryTokenStore(),
            limiter=RequestLimiter(per_second=1000, open_at_once=1000),
        )


@pytest.fixture
async def catalog() -> AsyncIterator[EpicCatalog]:
    async with httpx.AsyncClient() as http:
        yield EpicCatalog(http)


@pytest.fixture
async def epic(session: AsyncSession) -> Account:
    await seed_providers(session)
    account = await make_account(session, key="epic", external_account_id="0123456789abcdef")
    await session.commit()
    return account


async def owned_on_epic(
    session: AsyncSession, account: Account, namespace: str, title: str, item: str
) -> Work:
    work = await make_work(session, title)
    work.item_kind = ItemKind.GAME
    entitlement = Entitlement(
        account_id=account.id,
        provider_item_id=item,
        provider_title=title,
        raw_payload={"namespace": namespace, "catalogItemId": item},
    )
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    await session.flush()
    return work


async def run_epic(db: Database, igdb: IgdbClient, catalog: EpicCatalog) -> SyncStatus:
    return (await enrich(db, provider="igdb", step=anchor_epic_works(igdb, catalog))).status


@respx.mock
async def test_an_epic_game_is_anchored_through_its_namespace_s_base_game_offer(
    db: Database, session: AsyncSession, epic: Account, igdb: IgdbClient, catalog: EpicCatalog
) -> None:
    mount_catalog()
    Igdb([{"game": 1906, "uid": GONE_HOME_OFFER, "source": EPIC}], {1906: "Gone Home"}).mount()
    work = await owned_on_epic(
        session, epic, GONE_HOME_NS, "Gone Home", "48171393707541359f3a7dd7257b2757"
    )
    await session.commit()

    assert await run_epic(db, igdb, catalog) is SyncStatus.SUCCESS

    anchored = await work_of(db, work.id)
    assert (anchored.igdb_id, anchored.is_matched, anchored.title) == (1906, True, "Gone Home")
    layers = await read(db, lambda r: r.scalars(select(EntitlementWork.match_layer)))
    assert list(layers) == [MatchLayer.HARD_ID]


@respx.mock
async def test_beside_its_beta_only_the_work_titled_as_the_offer_is_anchored(
    db: Database, session: AsyncSession, epic: Account, igdb: IgdbClient, catalog: EpicCatalog
) -> None:
    """Killing Floor 2 and its beta share a namespace; the beta is not the game (rule 6)."""

    kf2 = next(o for o in recorded_offers(KF2_NS)["elements"] if o["offerType"] == "BASE_GAME")
    mount_catalog()
    Igdb([{"game": 7348, "uid": kf2["id"], "source": EPIC}], {7348: "Killing Floor 2"}).mount()
    game = await owned_on_epic(session, epic, KF2_NS, "Killing Floor 2", "a")
    beta = await owned_on_epic(session, epic, KF2_NS, "KillingFloor2Beta", "b")
    await session.commit()

    await run_epic(db, igdb, catalog)

    assert (await work_of(db, game.id)).igdb_id == 7348
    assert (await work_of(db, beta.id)).is_matched is False


@respx.mock
async def test_base_offers_naming_two_games_anchor_nothing(
    db: Database, session: AsyncSession, epic: Account, igdb: IgdbClient, catalog: EpicCatalog
) -> None:
    mount_catalog(
        {
            "ns": [
                {"id": "o1", "offerType": "BASE_GAME", "title": "A"},
                {"id": "o2", "offerType": "BASE_GAME", "title": "B"},
            ]
        }
    )
    Igdb(
        [{"game": 1, "uid": "o1", "source": EPIC}, {"game": 2, "uid": "o2", "source": EPIC}],
        {1: "A", 2: "B"},
    ).mount()
    work = await owned_on_epic(session, epic, "ns", "A", "a")
    await session.commit()

    await run_epic(db, igdb, catalog)

    assert (await work_of(db, work.id)).is_matched is False


@respx.mock
async def test_an_epic_copy_of_a_game_a_steam_work_holds_is_folded_into_it(
    db: Database,
    session: AsyncSession,
    epic: Account,
    igdb: IgdbClient,
    catalog: EpicCatalog,
) -> None:
    """One game owned on both platforms is one card."""

    steam = await make_account(session)
    mount_catalog()
    Igdb(
        [{"game": 1906, "uid": "237930"}, {"game": 1906, "uid": GONE_HOME_OFFER, "source": EPIC}],
        {1906: "Gone Home"},
    ).mount()
    on_steam = await own(session, steam, "237930", "Gone Home")
    on_epic = await owned_on_epic(session, epic, GONE_HOME_NS, "Gone Home", "4817")
    await session.commit()
    assert await anchor(db, igdb) is SyncStatus.SUCCESS

    await run_epic(db, igdb, catalog)

    assert await read(db, lambda r: r.get(Work, on_epic.id)) is None
    works = await read(db, lambda r: r.scalars(select(EntitlementWork.work_id)))
    assert set(works) == {on_steam.id}
    audit = await read(db, lambda r: r.scalar(select(MatchAudit)))
    assert (audit.action, audit.work_id) == (MatchAction.MERGED, on_steam.id)


@respx.mock
async def test_an_epic_only_game_keeps_igdb_s_steam_appid_for_its_score(
    db: Database, session: AsyncSession, epic: Account, igdb: IgdbClient, catalog: EpicCatalog
) -> None:
    """RAWG confirms a candidate by a Steam appid; an Epic-only game has none of its own."""

    mount_catalog()
    Igdb(
        [
            {"game": 1906, "uid": GONE_HOME_OFFER, "source": EPIC},
            {"game": 1906, "uid": "237930"},
        ],
        {1906: "Gone Home"},
    ).mount()
    work = await owned_on_epic(session, epic, GONE_HOME_NS, "Gone Home", "4817")
    await session.commit()

    await run_epic(db, igdb, catalog)

    rows = await read(
        db,
        lambda r: r.execute(
            select(ExternalId.entity_id, ExternalId.value, ExternalId.is_authoritative).where(
                ExternalId.namespace == "steam"
            )
        ),
    )
    assert rows.all() == [(work.id, "237930", True)]


@respx.mock
async def test_a_work_not_known_to_be_a_game_is_not_asked_about(
    db: Database, session: AsyncSession, epic: Account, igdb: IgdbClient, catalog: EpicCatalog
) -> None:
    offers = mount_catalog()
    Igdb([], {}).mount()
    work = await owned_on_epic(session, epic, GONE_HOME_NS, "Gone Home", "4817")
    work.item_kind = ItemKind.DLC
    await session.commit()

    await run_epic(db, igdb, catalog)

    assert not offers.called


@respx.mock
async def test_offers_are_paged_to_the_end_under_one_app_token(catalog: EpicCatalog) -> None:
    token = respx.post(epic_module.OAUTH).mock(return_value=httpx.Response(200, json=APP_TOKEN))
    pages = [
        {"elements": [{"id": f"o{n}"} for n in range(100)], "paging": {"total": 150}},
        {"elements": [{"id": f"o{n}"} for n in range(100, 150)], "paging": {"total": 150}},
    ]
    route = respx.get(url__startswith=f"{epic_module.CATALOG}/namespace/").mock(
        side_effect=lambda request: httpx.Response(
            200, json=pages[int(request.url.params["start"]) // 100]
        )
    )

    offers = await catalog.offers("ns")
    await catalog.offers("ns")

    assert len(offers) == 150
    assert route.call_count == 4
    assert token.call_count == 1
    sent = token.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in sent


@respx.mock
async def test_a_short_offer_listing_is_an_error(catalog: EpicCatalog) -> None:
    from ludarium.providers import MalformedResponseError

    respx.post(epic_module.OAUTH).mock(return_value=httpx.Response(200, json=APP_TOKEN))
    respx.get(url__startswith=f"{epic_module.CATALOG}/namespace/").mock(
        return_value=httpx.Response(200, json={"elements": [], "paging": {"total": 3}})
    )

    with pytest.raises(MalformedResponseError, match="0 of 3"):
        await catalog.offers("ns")


async def test_a_namespace_that_would_not_be_a_path_segment_is_refused(
    catalog: EpicCatalog,
) -> None:
    with pytest.raises(ValueError):
        await catalog.offers("../../account")
