import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_provider, make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.classification import STORE_TYPES, classify_steam_items, kind_of
from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import (
    EntitlementOrigin,
    EntityType,
    ItemKind,
    SourceKind,
    SyncStatus,
    WorkLinkRole,
)
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    FetchCache,
    FieldProvenance,
    Provider,
    SyncRun,
    Work,
)
from ludarium.providers import steam_store as store_module
from ludarium.providers.steam_store import SteamStoreClient
from ludarium.resolver import record, resolve

FIXTURES = Path(__file__).parent / "fixtures" / "steam_store"
GET_ITEMS_URL = f"{store_module.STORE_API}{store_module.GET_ITEMS}"

# What `items.json` says of each app it was recorded for.
RECORDED: dict[str, ItemKind | None] = {
    "1611740": ItemKind.PLAYTEST,  # BattleBit Remastered Playtest
    "35420": ItemKind.MOD,  # Defence Alliance 2
    "235900": ItemKind.TOOL,  # RPG Maker XP
    "594650": ItemKind.GAME,  # Hunt: Showdown 1896
    # The store has no word for a test client: the limit ADR-0020 records.
    "770720": ItemKind.GAME,  # Hunt: Showdown 1896 (Test Server)
    "378649": ItemKind.DLC,  # The Witcher 3: Wild Hunt - Hearts of Stone
    "323180": ItemKind.SOUNDTRACK,  # Portal 2 Soundtrack
    "35020": ItemKind.DEMO,  # Batman: Arkham Asylum Demo
    "227700": None,  # Firefall, delisted
}


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def asked_for(request: httpx.Request) -> list[int]:
    return [entry["appid"] for entry in json.loads(request.url.params["input_json"])["ids"]]


def the_store(request: httpx.Request) -> httpx.Response:
    """The recorded answer, cut down to the apps this request named, as the store would."""

    ids = asked_for(request)
    items = [
        item for item in recorded("items.json")["response"]["store_items"] if item["id"] in ids
    ]
    return httpx.Response(200, json={"response": {"store_items": items}})


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(store_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def store() -> AsyncIterator[SteamStoreClient]:
    async with httpx.AsyncClient() as client:
        yield SteamStoreClient(client)


@pytest.fixture
async def steam(session: AsyncSession) -> Account:
    account = await make_account(session)
    await make_provider(session, key="steam_store")
    await session.commit()
    return account


async def own(session: AsyncSession, account: Account, appid: str, title: str = "An app") -> Work:
    """An entitlement and the stub a sync would have made for it."""

    work = await make_work(session, title)
    entitlement = Entitlement(account_id=account.id, provider_item_id=appid, provider_title=title)
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    await session.flush()
    return work


async def kinds(db: Database) -> dict[int, ItemKind | None]:
    async with db.session_factory() as reader:
        return dict((await reader.execute(select(Work.id, Work.item_kind))).tuples().all())


@respx.mock
async def test_each_app_takes_the_kind_the_store_gives_it(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    works = {appid: await own(session, steam, appid) for appid in RECORDED}
    await session.commit()

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert run.status is SyncStatus.SUCCESS
    stored = await kinds(db)
    assert {appid: stored[work.id] for appid, work in works.items()} == RECORDED


@respx.mock
async def test_the_store_s_word_is_a_provenance_row_the_resolver_chose(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Rule 9: the step records, and the column is the resolver's."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "1611740")
    await session.commit()

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    row = (await session.scalars(select(FieldProvenance))).one()
    assert (row.entity_type, row.entity_id, row.field) == (EntityType.WORK, work.id, "item_kind")
    assert (row.source_kind, row.source_ref) == (SourceKind.PLATFORM_API, "steam_store")
    assert row.value == "playtest"
    assert row.run_id == run.id
    assert row.is_effective


@respx.mock
async def test_a_user_s_own_label_outlives_the_store(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Rule 3. Hunt's test server is the case: the store calls it a game, the user knows."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "770720")
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field="item_kind",
        source_kind=SourceKind.MANUAL,
        source_ref="manual",
        value=ItemKind.PLAYTEST.value,
    )
    await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=["item_kind"])
    await session.commit()

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert run.status is SyncStatus.SUCCESS
    assert (await kinds(db))[work.id] is ItemKind.PLAYTEST


@respx.mock
async def test_an_app_the_store_does_not_know_asserts_nothing(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Not a null row: under `precedence` one would outrank IGDB's answer with no answer."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    await own(session, steam, "227700", "Firefall")
    await session.commit()

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert run.status is SyncStatus.SUCCESS
    assert (await session.scalars(select(FieldProvenance))).all() == []


@respx.mock
async def test_a_kind_the_store_stops_answering_for_is_kept(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    route = respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "1611740")
    await session.commit()
    await enrich(db, provider="steam_store", step=classify_steam_items(store))
    delisted = {"id": 1611740, "success": 15, "appid": 0}
    # `side_effect`, not `return_value`: respx prefers the first when both are set.
    route.mock(
        side_effect=lambda _: httpx.Response(200, json={"response": {"store_items": [delisted]}})
    )
    await _expire_cache(db)

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert run.status is SyncStatus.SUCCESS
    assert route.call_count == 2
    assert (await kinds(db))[work.id] is ItemKind.PLAYTEST


@respx.mock
async def test_a_second_run_asks_the_store_nothing_and_changes_nothing(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    route = respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    for appid in RECORDED:
        await own(session, steam, appid)
    await session.commit()
    await enrich(db, provider="steam_store", step=classify_steam_items(store))
    first = await kinds(db)

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert route.call_count == 1
    assert (run.items_seen, run.items_updated) == (len(RECORDED), 0)
    assert await kinds(db) == first


async def test_a_library_larger_than_a_request_is_asked_about_two_hundred_at_a_time(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    appids = [str(appid) for appid in range(1_000_000, 1_000_000 + 450)]
    for appid in appids:
        await own(session, steam, appid)
    await session.commit()
    batches: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        ids = asked_for(request)
        batches.append(len(ids))
        items = [{"id": appid, "appid": appid, "success": 1, "type": 0} for appid in ids]
        return httpx.Response(200, json={"response": {"store_items": items}})

    with respx.mock:
        respx.get(GET_ITEMS_URL).mock(side_effect=answer)
        await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert batches == [200, 200, 50]
    assert set((await kinds(db)).values()) == {ItemKind.GAME}


@respx.mock
async def test_a_work_a_bundle_grants_keeps_its_own_kind(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """The store describes the app, which is the primary work. What it grants is not the app."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    bundle = await own(session, steam, "594650", "Hunt: Showdown 1896")
    granted = await make_work(session, "A soundtrack the bundle grants")
    entitlement = (await session.scalars(select(Entitlement))).one()
    session.add(
        EntitlementWork(
            entitlement_id=entitlement.id, work_id=granted.id, role=WorkLinkRole.GRANTED
        )
    )
    await session.commit()

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert run.status is SyncStatus.SUCCESS
    assert await kinds(db) == {bundle.id: ItemKind.GAME, granted.id: None}


@respx.mock
async def test_only_steam_entitlements_are_asked_about(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """The store knows Steam appids. A GOG id that happens to be digits is not one."""

    route = respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    await own(session, steam, "1611740")
    gog = await make_account(session, "gog", external_account_id="gog-user")
    await own(session, gog, "1495134320")
    session.add(
        Entitlement(
            account_id=steam.id,
            origin=EntitlementOrigin.MANUAL,
            provider_item_id=None,
            provider_title="A disc copy",
        )
    )
    await session.commit()

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert run.status is SyncStatus.SUCCESS
    assert asked_for(route.calls.last.request) == [1611740]


@respx.mock
async def test_a_store_outage_is_the_store_s_failure_and_classifies_nothing(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Rule 4: the library provider's health is not the store's to spend."""

    respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(503))
    work = await own(session, steam, "1611740")
    await session.commit()

    run = await enrich(db, provider="steam_store", step=classify_steam_items(store))

    assert run.status is SyncStatus.FAILED
    assert (await kinds(db))[work.id] is None
    async with db.session_factory() as reader:
        health = dict((await reader.execute(select(Provider.key, Provider.status))).tuples().all())
        runs = (await reader.scalars(select(SyncRun))).all()
    assert health["steam_store"] is SyncStatus.FAILED
    assert health["steam"] is SyncStatus.PENDING
    assert [(one.provider_id, one.account_id) for one in runs] == [(run.provider_id, None)]


@pytest.mark.parametrize(
    ("item", "kind"),
    [
        ({"type": 12}, ItemKind.PLAYTEST),
        ({"type": 99}, None),
        ({"type": True}, None),
        ({"type": "12"}, None),
        ({}, None),
        ([{"type": 0}], None),
        (None, None),
    ],
)
def test_only_a_code_that_was_measured_names_a_kind(item: Any, kind: ItemKind | None) -> None:
    assert kind_of(item) is kind


def test_every_measured_code_names_a_different_kind() -> None:
    assert len(set(STORE_TYPES.values())) == len(STORE_TYPES)


async def _expire_cache(db: Database) -> None:
    async with db.writing_session_factory() as writer:
        for row in await writer.scalars(select(FetchCache)):
            row.fetched_at = row.fetched_at.replace(year=2000)
        await writer.commit()
