import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import EntityType, ItemKind, MatchLayer, SourceKind, SyncStatus
from ludarium.matching import anchor_steam_works
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    ExternalId,
    FieldProvenance,
    Work,
)
from ludarium.providers import IgdbClient, IgdbCredentials, MemoryTokenStore, RequestLimiter
from ludarium.providers import igdb as igdb_module
from ludarium.resolver import record, resolve
from ludarium.seed import seed_providers

# `external_games` and `games` rows in the shape the live endpoints answer,
# measured on 2026-09-26. The ids are invented: IGDB data is not redistributed,
# so these are built per test rather than recorded. The token is the recorded
# one `test_igdb.py` uses.
TOKEN = json.loads((Path(__file__).parent / "fixtures" / "igdb" / "token.json").read_text())
EXTERNAL_GAMES_URL = f"{igdb_module.IGDB_API}/external_games"
GAMES_URL = f"{igdb_module.IGDB_API}/games"

type Rows = list[dict[str, Any]]


class Igdb:
    """IGDB as the step meets it: rows filtered by the query, cut at its limit and offset."""

    def __init__(self, external_games: Rows, games: dict[int, str]) -> None:
        self.external_games = [{"id": n, **row} for n, row in enumerate(external_games, 1)]
        self.games = games
        self.asked_appids: list[str] = []

    def answer_external_games(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        uids = re.findall(r'"([^"]+)"', body)
        self.asked_appids += uids
        limit, offset = _number(r"limit (\d+);", body), _number(r"offset (\d+);", body)
        rows = [row for row in self.external_games if row["uid"] in uids]
        return httpx.Response(200, json=rows[offset : offset + limit])

    def answer_games(self, request: httpx.Request) -> httpx.Response:
        found = re.search(r"id = \(([^)]*)\)", request.content.decode())
        assert found is not None
        ids = [int(n) for n in found[1].split(",")]
        return httpx.Response(
            200, json=[{"id": n, "name": self.games[n]} for n in ids if n in self.games]
        )

    def mount(self) -> None:
        respx.post(igdb_module.TWITCH_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
        respx.post(EXTERNAL_GAMES_URL).mock(side_effect=self.answer_external_games)
        respx.post(GAMES_URL).mock(side_effect=self.answer_games)


def _number(pattern: str, body: str) -> int:
    found = re.search(pattern, body)
    assert found is not None, body
    return int(found[1])


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(igdb_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(igdb_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def client() -> AsyncIterator[IgdbClient]:
    async with httpx.AsyncClient() as http:
        yield IgdbClient(
            IgdbCredentials(client_id="not-a-real-client-id", client_secret="not-a-real-secret"),
            http,
            tokens=MemoryTokenStore(),
            limiter=RequestLimiter(per_second=1000, open_at_once=1000),
        )


@pytest.fixture
async def steam(session: AsyncSession) -> Account:
    await seed_providers(session)
    account = await make_account(session)
    await session.commit()
    return account


async def own(
    session: AsyncSession,
    account: Account,
    appid: str,
    title: str = "A store's name",
    *,
    kind: ItemKind | None = ItemKind.GAME,
) -> Work:
    """A Steam entitlement and the stub a sync made for it, classified as `kind`."""

    work = await make_work(session, title)
    work.item_kind = kind
    entitlement = Entitlement(account_id=account.id, provider_item_id=appid, provider_title=title)
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    await session.flush()
    return work


async def anchor(db: Database, client: IgdbClient) -> SyncStatus:
    return (await enrich(db, provider="igdb", step=anchor_steam_works(client))).status


async def read[T](db: Database, query: Callable[[AsyncSession], Any]) -> T:
    async with db.session_factory() as reader:
        result: T = await query(reader)
        return result


async def work_of(db: Database, work_id: int) -> Work:
    return await read(db, lambda reader: reader.get_one(Work, work_id))


@respx.mock
async def test_a_game_is_anchored_to_the_igdb_game_behind_its_appid(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    Igdb([{"game": 1942, "uid": "292030"}], {1942: "The Witcher 3: Wild Hunt"}).mount()
    stub = await own(session, steam, "292030", "The Witcher® 3: Wild Hunt")
    await session.commit()

    assert await anchor(db, client) is SyncStatus.SUCCESS

    work = await work_of(db, stub.id)
    assert (work.igdb_id, work.is_matched) == (1942, True)
    assert (work.title, work.sort_title) == (
        "The Witcher 3: Wild Hunt",
        "Witcher 3: Wild Hunt, The",
    )
    anchors = await read(db, lambda reader: reader.scalars(select(ExternalId)))
    assert [
        (a.entity_type, a.entity_id, a.namespace, a.value, a.is_authoritative, a.source_ref)
        for a in anchors
    ] == [(EntityType.WORK, stub.id, "igdb", "1942", True, "igdb")]
    link = await read(db, lambda reader: reader.scalar(select(EntitlementWork)))
    assert link.match_layer is MatchLayer.HARD_ID
    title = await read(
        db,
        lambda reader: reader.scalar(
            select(FieldProvenance).where(FieldProvenance.field == "title")
        ),
    )
    assert (title.source_kind, title.source_ref, title.is_effective) == (
        SourceKind.METADATA_PROVIDER,
        "igdb",
        True,
    )


@pytest.mark.parametrize("kind", [None, ItemKind.PLAYTEST, ItemKind.DLC, ItemKind.DEMO])
@respx.mock
async def test_only_what_is_known_to_be_a_game_is_asked_about(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient, kind: ItemKind | None
) -> None:
    # The case #41 moved forward for: a test client's appid resolves to its game.
    igdb = Igdb([{"game": 16999, "uid": "931180"}], {16999: "Conan Exiles"})
    igdb.mount()
    stub = await own(session, steam, "931180", "Conan Exiles - Public Beta Client", kind=kind)
    await session.commit()

    assert await anchor(db, client) is SyncStatus.SUCCESS

    assert igdb.asked_appids == []
    assert (await work_of(db, stub.id)).is_matched is False


@respx.mock
async def test_an_appid_igdb_does_not_know_leaves_the_stub_and_is_not_asked_again(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    igdb = Igdb([], {})
    igdb.mount()
    stub = await own(session, steam, "999999999")
    await session.commit()

    await anchor(db, client)
    await anchor(db, client)

    work = await work_of(db, stub.id)
    assert (work.is_matched, work.igdb_id, work.title) == (False, None, "A store's name")
    assert igdb.asked_appids == ["999999999"]


@respx.mock
async def test_an_appid_two_igdb_games_claim_is_not_a_match(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    Igdb(
        [{"game": 10, "uid": "480490"}, {"game": 11, "uid": "480490"}], {10: "Prey", 11: "Prey"}
    ).mount()
    stub = await own(session, steam, "480490", "Prey")
    await session.commit()

    await anchor(db, client)

    assert (await work_of(db, stub.id)).is_matched is False


@respx.mock
async def test_two_stubs_for_one_game_anchor_the_older_and_leave_the_other_for_a_merge(
    db: Database,
    session: AsyncSession,
    steam: Account,
    client: IgdbClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    Igdb(
        [{"game": 1942, "uid": "292030"}, {"game": 1942, "uid": "499450"}],
        {1942: "The Witcher 3: Wild Hunt"},
    ).mount()
    older = await own(session, steam, "499450", "The Witcher 3: Wild Hunt - GOTY")
    younger = await own(session, steam, "292030", "The Witcher 3: Wild Hunt")
    await session.commit()

    with caplog.at_level(logging.INFO, logger="ludarium.matching"):
        assert await anchor(db, client) is SyncStatus.SUCCESS

    assert (await work_of(db, older.id)).igdb_id == 1942
    left = await work_of(db, younger.id)
    assert (left.is_matched, left.title) == (False, "The Witcher 3: Wild Hunt")
    assert "1 works name an IGDB game another work already holds" in caplog.text


@respx.mock
async def test_a_game_another_work_already_holds_is_left_alone(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    Igdb([{"game": 1942, "uid": "292030"}], {1942: "The Witcher 3: Wild Hunt"}).mount()
    holder = await make_work(session, "The Witcher 3: Wild Hunt")
    holder.igdb_id, holder.is_matched = 1942, True
    session.add(
        ExternalId(entity_type=EntityType.WORK, entity_id=holder.id, namespace="igdb", value="1942")
    )
    stub = await own(session, steam, "292030")
    await session.commit()

    assert await anchor(db, client) is SyncStatus.SUCCESS

    assert (await work_of(db, stub.id)).is_matched is False


@respx.mock
async def test_a_title_the_user_set_outranks_igdb(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    Igdb([{"game": 1942, "uid": "292030"}], {1942: "The Witcher 3: Wild Hunt"}).mount()
    stub = await own(session, steam, "292030")
    for field, value in {"title": "Wiedźmin 3", "sort_title": "Wiedźmin 3"}.items():
        await record(
            session,
            entity_type=EntityType.WORK,
            entity_id=stub.id,
            field=field,
            value=value,
            source_kind=SourceKind.MANUAL,
            source_ref="user",
        )
        await resolve(session, entity_type=EntityType.WORK, entity_id=stub.id, fields=[field])
    await session.commit()

    await anchor(db, client)

    work = await work_of(db, stub.id)
    assert (work.igdb_id, work.title, work.sort_title) == (1942, "Wiedźmin 3", "Wiedźmin 3")


@respx.mock
async def test_a_game_igdb_gives_no_name_is_anchored_and_keeps_the_store_s(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    Igdb([{"game": 1942, "uid": "292030"}], {}).mount()
    stub = await own(session, steam, "292030", "The Witcher 3: Wild Hunt")
    await session.commit()

    await anchor(db, client)

    work = await work_of(db, stub.id)
    assert (work.igdb_id, work.title) == (1942, "The Witcher 3: Wild Hunt")


@respx.mock
async def test_a_malformed_answer_fails_the_run_as_igdb_s_and_anchors_nothing(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    Igdb([{"uid": "292030"}], {}).mount()
    stub = await own(session, steam, "292030")
    await session.commit()

    assert await anchor(db, client) is SyncStatus.FAILED

    assert (await work_of(db, stub.id)).is_matched is False


@respx.mock
async def test_a_second_run_over_an_anchored_library_asks_igdb_nothing(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    igdb = Igdb([{"game": 1942, "uid": "292030"}], {1942: "The Witcher 3: Wild Hunt"})
    igdb.mount()
    await own(session, steam, "292030")
    await session.commit()

    await anchor(db, client)
    calls = len(respx.calls)
    await anchor(db, client)

    assert len(respx.calls) == calls


@respx.mock
async def test_a_games_row_without_an_id_fails_the_run_too(
    db: Database, session: AsyncSession, steam: Account, client: IgdbClient
) -> None:
    Igdb([{"game": 1942, "uid": "292030"}], {}).mount()
    respx.post(GAMES_URL).mock(return_value=httpx.Response(200, json=[{"name": "No id"}]))
    stub = await own(session, steam, "292030")
    await session.commit()

    assert await anchor(db, client) is SyncStatus.FAILED

    assert (await work_of(db, stub.id)).is_matched is False
