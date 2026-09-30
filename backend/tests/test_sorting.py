import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_account, sync_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api.works import _owned_works
from ludarium.config import Settings
from ludarium.models import Entitlement, EntitlementWork, UserWorkState, Work
from ludarium.sorting import COLUMNS, Direction, Ordering, Sort
from ludarium.titles import sort_title


@dataclass(frozen=True)
class Game:
    title: str
    metacritic: int | None = None
    steam: int | None = None
    released: date | None = None
    # None leaves the work with no state row, as a future write path might.
    playtime: int | None = 0
    played: datetime | None = None


def moment(day: int) -> datetime:
    return datetime(2026, 9, day, 20, 0, tzinfo=UTC)


LIBRARY = (
    Game("Celeste", 92, 97, date(2018, 1, 25), 900, moment(3)),
    Game("Dead Cells", 89, 97, date(2018, 8, 7), 30, moment(1)),
    Game("Hades", 93, 98, date(2020, 9, 17), 3000, moment(20)),
    Game("Minit", None, None, None, None),
    Game("Outer Wilds", 85, 95, date(2019, 5, 28), 0),
    Game("Portal 2", 95, 98, date(2011, 4, 18), 600, moment(2)),
    Game("Tunic", 85, None, date(2022, 3, 16), 0),
)


async def seed(session: AsyncSession, games: tuple[Game, ...] = LIBRARY) -> None:
    account = await make_account(session)
    for game in games:
        await add(session, account.id, game)
    await session.commit()


async def add(session: AsyncSession, account_id: int, game: Game) -> None:
    work = Work(
        title=game.title,
        sort_title=sort_title(game.title),
        metacritic_score=game.metacritic,
        steam_review_percent=game.steam,
        release_date=game.released,
        release_year=game.released.year if game.released else None,
    )
    session.add(work)
    await session.flush()
    entitlement = Entitlement(
        user_id=1,
        account_id=account_id,
        provider_item_id=str(work.id),
        provider_title=game.title,
    )
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    if game.playtime is not None:
        session.add(
            UserWorkState(
                user_id=1,
                work_id=work.id,
                playtime_minutes=game.playtime,
                last_played_at=game.played,
            )
        )


@pytest.fixture
async def library(client: TestClient, session: AsyncSession) -> TestClient:
    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    await seed(session)
    return client


def page(client: TestClient, **params: Any) -> dict[str, Any]:
    response = client.get("/api/works", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def listed(client: TestClient, **params: Any) -> list[str]:
    return [work["title"] for work in page(client, **params)["works"]]


ORDERS = [(sort, direction) for sort in Sort for direction in Direction]


def test_the_default_order_is_the_title(library: TestClient) -> None:
    assert listed(library) == listed(library, sort="title", order="asc")
    assert listed(library)[:2] == ["Celeste", "Dead Cells"]


def test_the_title_reverses(library: TestClient) -> None:
    assert listed(library, sort="title", order="desc") == listed(library)[::-1]


@pytest.mark.parametrize(
    ("sort", "order", "expected"),
    [
        (
            "metacritic",
            "desc",
            ["Portal 2", "Hades", "Celeste", "Dead Cells", "Outer Wilds", "Tunic", "Minit"],
        ),
        (
            "metacritic",
            "asc",
            ["Outer Wilds", "Tunic", "Dead Cells", "Celeste", "Hades", "Portal 2", "Minit"],
        ),
        (
            "steam_reviews",
            "desc",
            ["Hades", "Portal 2", "Celeste", "Dead Cells", "Outer Wilds", "Minit", "Tunic"],
        ),
        (
            "release_date",
            "desc",
            ["Tunic", "Hades", "Outer Wilds", "Dead Cells", "Celeste", "Portal 2", "Minit"],
        ),
        (
            "release_date",
            "asc",
            ["Portal 2", "Celeste", "Dead Cells", "Outer Wilds", "Hades", "Tunic", "Minit"],
        ),
        (
            "last_played",
            "desc",
            ["Hades", "Celeste", "Portal 2", "Dead Cells", "Minit", "Outer Wilds", "Tunic"],
        ),
    ],
)
def test_each_order_puts_the_unknown_last(
    library: TestClient, sort: str, order: str, expected: list[str]
) -> None:
    """Minit has no score, no date and no play: in both directions it is last, not worst."""

    assert listed(library, sort=sort, order=order) == expected


def test_a_tie_is_broken_by_title_a_to_z_in_both_directions(library: TestClient) -> None:
    """Outer Wilds and Tunic are both 85; best-first must not list the 85s from Z to A."""

    ascending = listed(library, sort="metacritic", order="asc")
    descending = listed(library, sort="metacritic", order="desc")

    assert ascending.index("Outer Wilds") < ascending.index("Tunic")
    assert descending.index("Outer Wilds") < descending.index("Tunic")


def test_a_missing_state_row_is_unknown_playtime_not_zero(library: TestClient) -> None:
    """Minit has no state row. Outer Wilds and Tunic have one, at 0 minutes."""

    ascending = listed(library, sort="playtime", order="asc")
    descending = listed(library, sort="playtime", order="desc")

    assert ascending[:2] == ["Outer Wilds", "Tunic"]
    assert ascending[-1] == "Minit"
    assert descending == [
        "Hades",
        "Celeste",
        "Portal 2",
        "Dead Cells",
        "Outer Wilds",
        "Tunic",
        "Minit",
    ]


@pytest.mark.parametrize(("sort", "order"), ORDERS)
def test_walking_the_cursor_meets_every_work_once_in_the_listed_order(
    library: TestClient, sort: str, order: str
) -> None:
    """One work per page, so every boundary is crossed: ties, the step into the nulls, the nulls."""

    whole = listed(library, sort=sort, order=order)
    walked: list[str] = []
    params: dict[str, Any] = {"sort": sort, "order": order, "limit": 1}
    # Bounded: a cursor that stops advancing would otherwise hang the suite
    # rather than fail it.
    for _ in range(len(whole) + 1):
        body = page(library, **params)
        walked += [work["title"] for work in body["works"]]
        if body["next_cursor"] is None:
            break
        params["cursor"] = body["next_cursor"]

    assert walked == whole


@pytest.mark.parametrize(
    ("issued", "replayed"),
    [
        ({"sort": "metacritic", "order": "desc"}, {"sort": "playtime", "order": "desc"}),
        ({"sort": "metacritic", "order": "desc"}, {"sort": "metacritic", "order": "asc"}),
        ({"sort": "title", "order": "asc"}, {"sort": "release_date", "order": "asc"}),
        ({"sort": "playtime", "order": "asc"}, {}),
    ],
)
def test_a_cursor_from_one_order_is_refused_under_another(
    library: TestClient, issued: dict[str, str], replayed: dict[str, str]
) -> None:
    """A Metacritic score of 89 read as 89 minutes played would page from nowhere in particular."""

    cursor = page(library, limit=2, **issued)["next_cursor"]

    response = library.get("/api/works", params={**replayed, "cursor": cursor})

    assert response.status_code == 400


def cursor_with(value: Any, sort: str = "last_played", order: str = "desc") -> str:
    return urlsafe_b64encode(json.dumps([3, sort, order, value, "celeste", 1]).encode()).decode()


@pytest.mark.parametrize(
    ("sort", "value"),
    [
        ("last_played", "2026-09-03T20:00:00"),
        ("last_played", "yesterday"),
        ("last_played", 1_725_000_000),
        ("release_date", "2018-01-25T00:00:00+00:00"),
        ("release_date", 2018),
        ("metacritic", 89.5),
        ("metacritic", True),
        ("metacritic", "89"),
        ("title", "celeste"),
    ],
)
def test_a_cursor_value_of_the_wrong_kind_is_refused(
    library: TestClient, sort: str, value: Any
) -> None:
    """A moment without a zone included: accepted, it would reach the bind and be a 500."""

    response = library.get(
        "/api/works",
        params={"sort": sort, "order": "desc", "cursor": cursor_with(value, sort)},
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("sort", "value", "work_id"),
    [
        ("metacritic", 10**30, 1),
        ("playtime", -(10**30), 1),
        ("last_played", "0001-01-01T00:00:00+05:00", 1),
        ("last_played", "9999-12-31T23:00:00-05:00", 1),
        ("title", None, 10**30),
    ],
)
def test_a_cursor_value_out_of_range_is_refused(
    library: TestClient, sort: str, value: Any, work_id: int
) -> None:
    """The right kind, and past what the database can bind: refused, not a 500 at the bind."""

    cursor = urlsafe_b64encode(
        json.dumps([3, sort, "desc", value, "celeste", work_id]).encode()
    ).decode()

    response = library.get("/api/works", params={"sort": sort, "order": "desc", "cursor": cursor})

    assert response.status_code == 400


def test_a_cursor_value_of_the_right_kind_is_read(library: TestClient) -> None:
    response = library.get(
        "/api/works",
        params={
            "sort": "last_played",
            "order": "desc",
            "cursor": cursor_with(moment(3).isoformat()),
        },
    )

    assert response.status_code == 200
    assert [work["title"] for work in response.json()["works"]][:2] == ["Portal 2", "Dead Cells"]


async def test_a_sync_landing_mid_scroll_neither_repeats_nor_skips(
    library: TestClient, session: AsyncSession
) -> None:
    """A new game ahead of the cursor is not re-read; one past it is met in its place."""

    first = page(library, sort="metacritic", order="desc", limit=3)
    account_id = (await session.get(Entitlement, 1)).account_id  # type: ignore[union-attr]
    await add(session, account_id, Game("Braid", metacritic=99))
    await add(session, account_id, Game("Inside", metacritic=87))
    await session.commit()

    rest = listed(library, sort="metacritic", order="desc", cursor=first["next_cursor"])

    assert [work["title"] for work in first["works"]] == ["Portal 2", "Hades", "Celeste"]
    assert rest == ["Dead Cells", "Inside", "Outer Wilds", "Tunic", "Minit"]


def test_a_filter_and_an_order_page_together(library: TestClient) -> None:
    first = page(library, sort="release_date", order="desc", metacritic_min=89, limit=2)
    rest = listed(
        library,
        sort="release_date",
        order="desc",
        metacritic_min=89,
        cursor=first["next_cursor"],
    )

    assert [work["title"] for work in first["works"]] + rest == [
        "Hades",
        "Dead Cells",
        "Celeste",
        "Portal 2",
    ]


@pytest.mark.parametrize("params", [{"sort": "rating"}, {"order": "up"}, {"sort": ""}])
def test_an_order_the_api_does_not_offer_is_refused(
    library: TestClient, params: dict[str, str]
) -> None:
    assert library.get("/api/works", params=params).status_code == 422


def test_the_cursor_names_its_order(library: TestClient) -> None:
    cursor = page(library, sort="release_date", order="asc", limit=1)["next_cursor"]

    assert json.loads(urlsafe_b64decode(cursor)) == [
        3,
        "release_date",
        "asc",
        "2011-04-18",
        "portal 2",
        6,
    ]


@pytest.mark.parametrize(
    ("sort", "order"),
    [
        (sort, direction)
        for sort, direction in ORDERS
        if sort not in COLUMNS or COLUMNS[sort].class_ is Work
    ],
)
def test_each_order_over_a_work_column_is_read_from_an_index(
    settings: Settings, library: TestClient, sort: Sort, order: Direction
) -> None:
    """Measured at 42 ms a page on 20,000 works without, 1.1 ms with (#94).

    A change to how the order is written — `NULLS LAST` instead of `IS NULL`,
    a descending tie — stops matching the index without failing any other test.
    Playtime and last played are on the outer-joined state row, and sort by design.
    """

    ordering = Ordering(Sort(sort), Direction(order))
    engine = create_engine(sync_url(settings.database_url))
    query = _owned_works(1).order_by(*ordering.order_by()).limit(101)
    compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
    with engine.connect() as connection:
        plan = [row[3] for row in connection.execute(text(f"EXPLAIN QUERY PLAN {compiled}"))]
    engine.dispose()

    assert not any("TEMP B-TREE" in step for step in plan), plan
