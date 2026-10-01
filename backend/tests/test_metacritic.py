import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import metacritic as metacritic_module
from ludarium import queries
from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import EntityType, SourceKind, SyncStatus
from ludarium.metacritic import score_matched_works
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    ExternalId,
    FieldProvenance,
    Provider,
    Work,
)
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


@respx.mock
async def test_a_candidate_rawg_has_since_removed_is_passed_over(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    """A search cached for a month can name a game RAWG has since dropped or merged."""

    respx.get(f"{GAMES}/11/stores").mock(return_value=httpx.Response(404, json={}))
    Rawg(
        {"Prey": [11, 22], "Portal 2": [33]},
        {22: [steam("480490")], 33: [steam("620")]},
        {22: {"slug": "prey", "metacritic": 79}, 33: {"slug": "portal-2", "metacritic": 95}},
    ).mount()
    prey = await matched(session, steam_account, "480490", "Prey")
    portal = await matched(session, steam_account, "620", "Portal 2")
    await session.commit()

    assert await score(db, client) is SyncStatus.SUCCESS

    assert (await scored(db, prey.id))[0] == 79
    assert (await scored(db, portal.id))[0] == 95


@respx.mock
async def test_a_game_owned_only_elsewhere_is_confirmed_by_igdb_s_steam_appid(
    db: Database, session: AsyncSession, steam_account: Account, client: RawgClient
) -> None:
    """An Epic-only game has no Steam copy; layer 1 keeps IGDB's appid for it (#74)."""

    Rawg(
        {"Gone Home": [9]}, {9: [steam("237930")]}, {9: {"slug": "gone-home", "metacritic": 86}}
    ).mount()
    work = await make_work(session, "Gone Home")
    work.is_matched = True
    session.add(
        ExternalId(
            entity_type=EntityType.WORK,
            entity_id=work.id,
            namespace="steam",
            value="237930",
            is_authoritative=True,
        )
    )
    await session.commit()

    assert await score(db, client) is SyncStatus.SUCCESS

    assert (await scored(db, work.id))[0] == 86


async def slugs(db: Database) -> dict[int, str]:
    async with db.session_factory() as reader:
        rows = await reader.execute(
            select(ExternalId.entity_id, ExternalId.value).where(ExternalId.namespace == "rawg")
        )
        return dict(rows.tuples().all())


async def record_scores(db: Database, records: dict[int, dict[str, object]]) -> None:
    """`_record` on its own: which work holds which slug is decided there, not by the search."""

    async with db.session_factory() as reader:
        rawg = await reader.scalar(select(Provider.id).where(Provider.key == "rawg"))
    run = SimpleNamespace(database=db, provider_id=rawg, id=None)
    confirmed = {work_id: 7000 + work_id for work_id in records}
    await metacritic_module._record(
        run,  # type: ignore[arg-type]
        confirmed,
        {str(confirmed[work_id]): record for work_id, record in records.items()},
    )


async def test_a_slug_another_work_holds_stays_with_it(
    db: Database, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Two works confirmed to one RAWG game is a merge the matcher missed, not a second owner."""

    await seed_providers(session)
    first, second = await make_work(session, "One"), await make_work(session, "Two")
    await session.commit()

    await record_scores(db, {first.id: {"slug": "prey"}})
    await record_scores(db, {second.id: {"slug": "prey"}})

    assert await slugs(db) == {first.id: "prey"}
    assert "already work" in caplog.text


async def test_a_slug_rawg_changed_replaces_the_old_one(
    db: Database, session: AsyncSession
) -> None:
    await seed_providers(session)
    work = await make_work(session)
    await session.commit()

    await record_scores(db, {work.id: {"slug": "the-witcher-3"}})
    await record_scores(db, {work.id: {"slug": "the-witcher-3-wild-hunt"}})

    assert await slugs(db) == {work.id: "the-witcher-3-wild-hunt"}


async def test_a_slug_given_up_is_free_for_a_later_work_in_the_same_run(
    db: Database, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run goes in order, batches or not: one work moves on, the next takes its slug.

    And of two works given one new slug in a run, the first keeps it.
    """

    monkeypatch.setattr(queries, "BIND_LIMIT", 2)
    await seed_providers(session)
    moving, taking, first, second = [
        await make_work(session, title) for title in ("A", "B", "C", "D")
    ]
    await session.commit()
    await record_scores(db, {moving.id: {"slug": "old"}})

    await record_scores(
        db,
        {
            moving.id: {"slug": "new"},
            taking.id: {"slug": "old"},
            first.id: {"slug": "shared"},
            second.id: {"slug": "shared"},
        },
    )

    assert await slugs(db) == {moving.id: "new", taking.id: "old", first.id: "shared"}
