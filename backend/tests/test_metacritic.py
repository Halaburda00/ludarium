import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import EntityType, SourceKind, SyncStatus
from ludarium.metacritic import score_matched_works
from ludarium.models import Account, Entitlement, EntitlementWork, ExternalId, FieldProvenance, Work
from ludarium.providers import RawgClient
from ludarium.providers import rawg as rawg_module
from ludarium.resolver import record, resolve
from ludarium.seed import seed_providers

# Invented, in the shapes RAWG's OpenAPI document gives. RAWG data may not be
# redistributed, so none is recorded here.
GAMES = f"{rawg_module.RAWG_API}/games"


class Rawg:
    """RAWG as the step meets it: search by name, stores by id, a record by id."""

    def __init__(
        self,
        searches: dict[str, list[int]],
        stores: dict[int, list[str]],
        games: dict[int, dict[str, Any]],
    ) -> None:
        self.searches, self.stores, self.games = searches, stores, games
        self.asked: list[str] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/")
        self.asked.append(path if path != "games" else f"search:{request.url.params['search']}")
        if path == "games":
            ids = self.searches.get(request.url.params["search"], [])
            return httpx.Response(200, json={"results": [{"id": game} for game in ids]})
        if found := re.fullmatch(r"games/(\d+)/stores", path):
            links = self.stores.get(int(found[1]), [])
            return httpx.Response(200, json={"results": [{"url": url} for url in links]})
        if found := re.fullmatch(r"games/(\d+)", path):
            game = int(found[1])
            if game not in self.games:
                return httpx.Response(404, json={"detail": "Not found."})
            return httpx.Response(200, json={"id": game, **self.games[game]})
        raise AssertionError(f"unexpected request {path}")

    def mount(self) -> "Rawg":
        respx.get(url__startswith=GAMES).mock(side_effect=self.answer)
        return self


def steam(appid: str) -> str:
    return f"https://store.steampowered.com/app/{appid}/Some_Slug/"


WITCHER = {"slug": "the-witcher-3-wild-hunt", "metacritic": 92}
WITCHER_URL = "https://www.metacritic.com/game/pc/the-witcher-3-wild-hunt"


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rawg_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(rawg_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def client() -> AsyncIterator[RawgClient]:
    async with httpx.AsyncClient() as http:
        yield RawgClient("not-a-real-rawg-key", http)


@pytest.fixture
async def steam_account(session: AsyncSession) -> Account:
    await seed_providers(session)
    account = await make_account(session)
    await session.commit()
    return account


async def matched(
    session: AsyncSession, account: Account, appid: str, name: str, *, store_name: str = "x"
) -> Work:
    """A Steam game layer 1 has anchored, with IGDB's name recorded as it records it."""

    work = await make_work(session, store_name)
    work.is_matched = True
    entitlement = Entitlement(account_id=account.id, provider_item_id=appid, provider_title=name)
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field="title",
        source_kind=SourceKind.METADATA_PROVIDER,
        source_ref="igdb",
        value=name,
    )
    await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=["title"])
    await session.flush()
    return work


async def score(db: Database, client: RawgClient) -> SyncStatus:
    return (await enrich(db, provider="rawg", step=score_matched_works(client))).status


async def scored(db: Database, work_id: int) -> tuple[int | None, str | None, str | None]:
    async with db.session_factory() as reader:
        work = await reader.get_one(Work, work_id)
        slug = await reader.scalar(
            select(ExternalId.value).where(
                ExternalId.namespace == "rawg", ExternalId.entity_id == work_id
            )
        )
        return work.metacritic_score, work.metacritic_url, slug


@respx.mock
async def test_a_candidate_sold_under_the_work_s_appid_gives_it_its_score(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    Rawg(
        {"The Witcher 3: Wild Hunt": [3328]},
        {3328: [steam("292030")]},
        {3328: {**WITCHER, "metacritic_url": WITCHER_URL}},
    ).mount()
    work = await matched(session, steam_account, "292030", "The Witcher 3: Wild Hunt")
    await session.commit()

    assert await score(db, client) is SyncStatus.SUCCESS

    assert await scored(db, work.id) == (92, WITCHER_URL, "the-witcher-3-wild-hunt")
    async with db.session_factory() as reader:
        winners = await reader.scalars(
            select(FieldProvenance.source_ref).where(
                FieldProvenance.field == "metacritic_score", FieldProvenance.is_effective
            )
        )
        assert winners.all() == ["rawg"]


@respx.mock
async def test_a_namesake_sold_under_another_appid_is_passed_over(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    """Prey (2006) and Prey (2017): one name, two games, and only the appid tells them apart."""

    rawg = Rawg(
        {"Prey": [11, 22]},
        {11: [steam("3970")], 22: [steam("480490")]},
        {11: {"slug": "prey-2006", "metacritic": 79}, 22: {"slug": "prey", "metacritic": 79}},
    ).mount()
    work = await matched(session, steam_account, "480490", "Prey")
    await session.commit()

    assert await score(db, client) is SyncStatus.SUCCESS

    assert (await scored(db, work.id))[2] == "prey"
    assert rawg.asked == ["search:Prey", "games/11/stores", "games/22/stores", "games/22"]


@respx.mock
async def test_no_candidate_sold_under_the_appid_means_no_score(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    Rawg({"Prey": [11]}, {11: [steam("3970"), "https://www.gog.com/game/prey"]}, {}).mount()
    work = await matched(session, steam_account, "480490", "Prey")
    await session.commit()

    assert await score(db, client) is SyncStatus.SUCCESS

    assert await scored(db, work.id) == (None, None, None)


@respx.mock
async def test_the_search_uses_igdb_s_name_and_not_the_user_s(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    rawg = Rawg({}, {}, {}).mount()
    work = await matched(session, steam_account, "292030", "The Witcher 3: Wild Hunt")
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field="title",
        source_kind=SourceKind.MANUAL,
        source_ref="user",
        value="Wiedźmin 3",
    )
    await session.commit()

    await score(db, client)

    assert rawg.asked == ["search:The Witcher 3: Wild Hunt"]


@respx.mock
async def test_an_unmatched_stub_is_not_asked_about(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    rawg = Rawg({}, {}, {}).mount()
    work = await matched(session, steam_account, "292030", "The Witcher 3: Wild Hunt")
    work.is_matched = False
    await session.commit()

    await score(db, client)

    assert rawg.asked == []


@respx.mock
async def test_a_game_without_a_score_clears_the_old_one(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    Rawg(
        {"Prey": [22]}, {22: [steam("480490")]}, {22: {"slug": "prey", "metacritic": None}}
    ).mount()
    work = await matched(session, steam_account, "480490", "Prey")
    work.metacritic_score = 50
    await session.commit()

    await score(db, client)

    assert await scored(db, work.id) == (None, None, "prey")


@pytest.mark.parametrize(
    "record_",
    [
        {"slug": "prey", "metacritic": 101, "metacritic_url": "javascript:alert(1)"},
        {"slug": "prey", "metacritic": True, "metacritic_url": "http://example.com"},
    ],
    ids=["out of range and not https", "a bool and plain http"],
)
@respx.mock
async def test_what_is_not_a_score_or_a_safe_link_is_not_kept(
    db: Database,
    session: AsyncSession,
    steam_account: Account,
    client: RawgClient,
    record_: dict[str, Any],
) -> None:
    Rawg({"Prey": [22]}, {22: [steam("480490")]}, {22: record_}).mount()
    work = await matched(session, steam_account, "480490", "Prey")
    await session.commit()

    await score(db, client)

    assert (await scored(db, work.id))[:2] == (None, None)


@respx.mock
async def test_a_second_run_asks_rawg_nothing(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    rawg = Rawg({"Prey": [22]}, {22: [steam("480490")]}, {22: {"slug": "prey"}}).mount()
    await matched(session, steam_account, "480490", "Prey")
    await session.commit()

    await score(db, client)
    first = len(rawg.asked)
    await score(db, client)

    assert (first, len(rawg.asked)) == (3, 3)


@respx.mock
async def test_a_rawg_outage_fails_the_run_as_rawg_s(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    respx.get(url__startswith=GAMES).mock(return_value=httpx.Response(503))
    work = await matched(session, steam_account, "480490", "Prey")
    await session.commit()

    assert await score(db, client) is SyncStatus.FAILED

    assert await scored(db, work.id) == (None, None, None)
