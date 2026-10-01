import re
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
import respx
from conftest import make_work
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from test_matching import TOKEN

from ludarium import details as details_module
from ludarium import queries
from ludarium.db import Database
from ludarium.details import describe_matched_works, released_on
from ludarium.enrichment import enrich
from ludarium.enums import CompanyRole, EntityType, SourceKind, SyncStatus
from ludarium.models import (
    Company,
    ExternalId,
    FieldProvenance,
    Genre,
    Work,
    WorkCompany,
    WorkGenre,
)
from ludarium.providers import IgdbClient, IgdbCredentials, MemoryTokenStore, RequestLimiter
from ludarium.providers import igdb as igdb_module
from ludarium.resolver import record, resolve
from ludarium.seed import seed_providers

# Invented ids, names and text in IGDB's published shape: IGDB's data may not be
# redistributed, so no response of its is recorded here.
GAMES_URL = f"{igdb_module.IGDB_API}/games"
# 2015-05-19T00:00:00Z
MAY_2015 = 1431993600

GAMES: dict[int, dict[str, Any]] = {
    9001: {
        "id": 9001,
        "summary": "  A monster hunter looks for his adopted daughter.  ",
        "first_release_date": MAY_2015,
        "involved_companies": [
            {
                "id": 1,
                "company": {"id": 501, "name": "Studio Red"},
                "developer": True,
                "publisher": True,
                "porting": False,
                "supporting": False,
            },
            {
                "id": 2,
                "company": {"id": 502, "name": "Port House"},
                "developer": False,
                "publisher": False,
                "porting": True,
                "supporting": False,
            },
        ],
        "genres": [
            {"id": 31, "slug": "adventure", "name": "Adventure"},
            {"id": 32, "slug": "puzzle", "name": "Puzzle"},
        ],
    },
    9002: {"id": 9002},
}


class Igdb:
    def __init__(self, games: dict[int, dict[str, Any]]) -> None:
        self.games = games
        self.bodies: list[str] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        self.bodies.append(body)
        found = re.search(r"where id = \(([^)]*)\)", body)
        assert found is not None
        ids = [int(game) for game in found[1].split(",")]
        return httpx.Response(200, json=[self.games[game] for game in ids if game in self.games])

    def mount(self) -> "Igdb":
        respx.post(igdb_module.TWITCH_TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN))
        respx.post(GAMES_URL).mock(side_effect=self.answer)
        return self


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


@pytest.fixture(autouse=True)
async def seeded(session: AsyncSession) -> None:
    await seed_providers(session)


async def anchored(session: AsyncSession, game: int, title: str = "A game") -> Work:
    work = await make_work(session, title)
    work.igdb_id, work.is_matched = game, True
    session.add(
        ExternalId(
            entity_type=EntityType.WORK, entity_id=work.id, namespace="igdb", value=str(game)
        )
    )
    await session.commit()
    return work


async def describe(db: Database, client: IgdbClient) -> SyncStatus:
    return (await enrich(db, provider="igdb", step=describe_matched_works(client))).status


async def fetched(db: Database, work_id: int) -> Work:
    async with db.session_factory() as reader:
        return await reader.get_one(Work, work_id)


async def credits(db: Database, work_id: int) -> set[tuple[str, CompanyRole, str | None]]:
    async with db.session_factory() as reader:
        rows = await reader.execute(
            select(Company.name, WorkCompany.role, WorkCompany.source_ref)
            .join(Company, Company.id == WorkCompany.company_id)
            .where(WorkCompany.work_id == work_id)
        )
        return {(name, role, source) for name, role, source in rows}


@respx.mock
async def test_a_matched_work_gets_its_summary_release_date_and_year(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    Igdb(GAMES).mount()
    work = await anchored(session, 9001)

    assert await describe(db, client) is SyncStatus.SUCCESS

    described = await fetched(db, work.id)
    assert described.summary == "A monster hunter looks for his adopted daughter."
    assert described.release_date == date(2015, 5, 19)
    assert described.release_year == 2015


@respx.mock
async def test_each_company_is_credited_in_every_role_igdb_gives_it(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    Igdb(GAMES).mount()
    work = await anchored(session, 9001)

    await describe(db, client)

    assert await credits(db, work.id) == {
        ("Studio Red", CompanyRole.DEVELOPER, "igdb"),
        ("Studio Red", CompanyRole.PUBLISHER, "igdb"),
        ("Port House", CompanyRole.PORTING, "igdb"),
    }


@respx.mock
async def test_the_values_are_igdb_s_provenance_and_a_user_s_own_wins(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    """Rules 9 and 3: the step records, the resolver decides, the user outranks IGDB."""

    Igdb(GAMES).mount()
    work = await anchored(session, 9001)
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field="release_year",
        source_kind=SourceKind.MANUAL,
        source_ref="manual",
        value=2016,
    )
    await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=["release_year"])
    await session.commit()

    await describe(db, client)

    assert (await fetched(db, work.id)).release_year == 2016
    rows = (
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_id == work.id, FieldProvenance.source_ref == "igdb"
            )
        )
    ).all()
    assert {row.field for row in rows} == {"summary", "release_date", "release_year"}
    assert {row.source_kind for row in rows} == {SourceKind.METADATA_PROVIDER}


async def genres(db: Database, work_id: int) -> set[tuple[str, str, str | None]]:
    async with db.session_factory() as reader:
        rows = await reader.execute(
            select(Genre.slug, Genre.name, WorkGenre.source_ref)
            .join(Genre, Genre.id == WorkGenre.genre_id)
            .where(WorkGenre.work_id == work_id)
        )
        return {(slug, name, source) for slug, name, source in rows}


@respx.mock
async def test_a_matched_work_gets_its_genres_with_igdb_as_their_source(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    igdb = Igdb(GAMES).mount()
    work = await anchored(session, 9001)

    await describe(db, client)

    assert await genres(db, work.id) == {
        ("adventure", "Adventure", "igdb"),
        ("puzzle", "Puzzle", "igdb"),
    }
    assert "genres.slug" in igdb.bodies[0] and "genres.name" in igdb.bodies[0]


@respx.mock
async def test_a_second_run_replaces_igdb_s_genres_and_leaves_anyone_else_s(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    igdb = Igdb(GAMES).mount()
    work = await anchored(session, 9001)
    await describe(db, client)
    own = Genre(slug="cosy", name="Cosy")
    session.add(own)
    await session.flush()
    session.add(WorkGenre(work_id=work.id, genre_id=own.id, source_ref="manual"))
    await session.commit()
    # IGDB reconsiders: not a puzzle game, and "Adventure" is now worded differently.
    igdb.games = {
        9001: {**GAMES[9001], "genres": [{"id": 31, "slug": "adventure", "name": "Adventures"}]}
    }
    await session.execute(text("DELETE FROM fetch_cache WHERE resource = 'games/details'"))
    await session.commit()

    await describe(db, client)

    assert await genres(db, work.id) == {
        ("adventure", "Adventures", "igdb"),
        ("cosy", "Cosy", "manual"),
    }


@respx.mock
async def test_one_genre_is_one_row_however_many_games_have_it(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    Igdb({**GAMES, 9003: {**GAMES[9001], "id": 9003}}).mount()
    await anchored(session, 9001, "One")
    await anchored(session, 9003, "Two")

    await describe(db, client)

    assert (await session.scalars(select(Genre.slug).order_by(Genre.slug))).all() == [
        "adventure",
        "puzzle",
    ]


@respx.mock
async def test_a_game_igdb_says_nothing_about_asserts_nothing(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    Igdb(GAMES).mount()
    work = await anchored(session, 9002)

    assert await describe(db, client) is SyncStatus.SUCCESS

    assert (await session.scalars(select(FieldProvenance))).all() == []
    assert await credits(db, work.id) == set()
    assert await genres(db, work.id) == set()


@respx.mock
async def test_a_second_run_replaces_igdb_s_credits_and_leaves_anyone_else_s(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    igdb = Igdb(GAMES).mount()
    work = await anchored(session, 9001)
    await describe(db, client)
    own = Company(name="Someone I know")
    session.add(own)
    await session.flush()
    session.add(
        WorkCompany(
            work_id=work.id, company_id=own.id, role=CompanyRole.SUPPORT, source_ref="manual"
        )
    )
    await session.commit()
    # IGDB corrects the record: the porter was never involved.
    igdb.games = {
        9001: {**GAMES[9001], "involved_companies": GAMES[9001]["involved_companies"][:1]}
    }
    await session.execute(text("DELETE FROM fetch_cache WHERE resource = 'games/details'"))
    await session.commit()

    await describe(db, client)

    assert await credits(db, work.id) == {
        ("Studio Red", CompanyRole.DEVELOPER, "igdb"),
        ("Studio Red", CompanyRole.PUBLISHER, "igdb"),
        ("Someone I know", CompanyRole.SUPPORT, "manual"),
    }


@respx.mock
async def test_one_company_is_one_row_however_many_games_credit_it(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    Igdb({**GAMES, 9003: {**GAMES[9001], "id": 9003}}).mount()
    await anchored(session, 9001, "One")
    await anchored(session, 9003, "Two")

    await describe(db, client)

    names = (await session.scalars(select(Company.name).order_by(Company.name))).all()
    assert names == ["Port House", "Studio Red"]


@respx.mock
async def test_a_second_run_inside_a_month_asks_igdb_nothing(
    db: Database, session: AsyncSession, client: IgdbClient
) -> None:
    igdb = Igdb(GAMES).mount()
    await anchored(session, 9001)

    await describe(db, client)
    await describe(db, client)

    assert len(igdb.bodies) == 1


@respx.mock
async def test_works_are_committed_a_batch_at_a_time(
    db: Database, session: AsyncSession, client: IgdbClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that fails part-way keeps the batches it wrote, and held the lock for one each."""

    monkeypatch.setattr(queries, "BIND_LIMIT", 2)
    games = {game: {**GAMES[9001], "id": game} for game in range(9101, 9106)}
    Igdb(games).mount()
    works = [await anchored(session, game, f"Game {game}") for game in games]
    classify = details_module._classify

    async def failing(
        session: AsyncSession, source: str, listed: dict[int, object], *args: Any
    ) -> None:
        if works[4].id in listed:
            raise RuntimeError("the last batch fails")
        await classify(session, source, listed, *args)

    monkeypatch.setattr(details_module, "_classify", failing)

    with pytest.raises(RuntimeError):
        await describe(db, client)

    summaries = [(await fetched(db, work.id)).summary for work in works]
    assert summaries[:4] == ["A monster hunter looks for his adopted daughter."] * 4
    assert summaries[4] is None
    names = (await session.scalars(select(Company.name).order_by(Company.name))).all()
    assert names == ["Port House", "Studio Red"]


def test_a_release_date_is_taken_in_utc() -> None:
    """A game released at midnight UTC on New Year's Day is that year's, wherever the server is."""

    moment = released_on(1420070400)  # 2015-01-01T00:00:00Z
    assert moment is not None
    assert (moment.year, moment.date()) == (2015, date(2015, 1, 1))
    assert released_on(None) is None
    assert released_on(10**20) is None
