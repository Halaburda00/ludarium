from dataclasses import dataclass
from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_account, make_user
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import ItemKind, SteamRating
from ludarium.filters import LibraryFilters, Predicate
from ludarium.models import Account, Entitlement, EntitlementWork, UserWorkState, Work
from ludarium.models.types import utcnow
from ludarium.titles import sort_title


@dataclass(frozen=True)
class Game:
    title: str
    platforms: tuple[str, ...] = ("steam",)
    kind: ItemKind | None = ItemKind.GAME
    metacritic: int | None = None
    # Percent positive, and the store's verdict on it: None where there were
    # too few reviews for one, while the percentage is still there.
    steam: int | None = None
    verdict: SteamRating | None = None
    year: int | None = None
    # None leaves the work with no state row, as a future write path might.
    playtime: int | None = 0
    # Platforms whose copy is removed rather than live.
    removed_on: tuple[str, ...] = ()


LIBRARY = (
    Game(
        "Celeste",
        ("steam", "epic"),
        metacritic=92,
        steam=97,
        verdict=SteamRating.OVERWHELMINGLY_POSITIVE,
        year=2018,
        playtime=900,
    ),
    Game(
        "Dead Cells",
        ("epic",),
        metacritic=89,
        steam=93,
        verdict=SteamRating.VERY_POSITIVE,
        year=2018,
        playtime=30,
    ),
    Game(
        "Hades",
        ("steam",),
        metacritic=93,
        steam=98,
        verdict=SteamRating.OVERWHELMINGLY_POSITIVE,
        year=2020,
        playtime=3000,
    ),
    # 100% of a handful of reviews: a percentage and no verdict.
    Game("Hollow Knight: Soundtrack", kind=ItemKind.SOUNDTRACK, steam=100, year=2017),
    Game("Minit", ("epic",), metacritic=None, year=None, playtime=None),
    Game(
        "Portal 2",
        ("steam", "epic"),
        metacritic=95,
        steam=98,
        verdict=SteamRating.OVERWHELMINGLY_POSITIVE,
        year=2011,
        removed_on=("epic",),
    ),
    Game("Unsorted Stub", kind=None),
)


async def seed(session: AsyncSession, games: tuple[Game, ...] = LIBRARY) -> None:
    accounts: dict[str, Account] = {}
    for key, external in (("steam", "765611979"), ("epic", "0123456789abcdef")):
        accounts[key] = await make_account(session, key, external_account_id=external)
    for game in games:
        work = Work(
            title=game.title,
            sort_title=sort_title(game.title),
            item_kind=game.kind,
            metacritic_score=game.metacritic,
            steam_review_percent=game.steam,
            steam_review_rating=game.verdict,
            release_year=game.year,
        )
        session.add(work)
        await session.flush()
        for platform in game.platforms:
            entitlement = Entitlement(
                user_id=1,
                account_id=accounts[platform].id,
                provider_item_id=f"{platform}-{work.id}",
                provider_title=game.title,
                removed_at=utcnow() if platform in game.removed_on else None,
            )
            session.add(entitlement)
            await session.flush()
            session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
        if game.playtime is not None:
            session.add(UserWorkState(user_id=1, work_id=work.id, playtime_minutes=game.playtime))
    await session.commit()


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


def listed(client: TestClient, **params: Any) -> list[str]:
    response = client.get("/api/works", params=params)
    assert response.status_code == 200, response.text
    return [work["title"] for work in response.json()["works"]]


def test_no_filter_narrows_nothing(library: TestClient) -> None:
    assert len(listed(library)) == len(LIBRARY)


def test_a_platform_is_a_live_copy_there(library: TestClient) -> None:
    """Portal 2's Epic copy was removed, so its Steam copy does not make it an Epic game."""

    assert listed(library, platform="epic") == ["Celeste", "Dead Cells", "Minit"]


def test_several_platforms_are_any_of_them(library: TestClient) -> None:
    assert listed(library, platform=["epic", "steam"]) == listed(library)


def test_a_kind_leaves_out_the_unclassified(library: TestClient) -> None:
    assert listed(library, kind="soundtrack") == ["Hollow Knight: Soundtrack"]
    assert "Unsorted Stub" not in listed(library, kind=["game", "dlc"])


def test_a_score_range_leaves_out_the_unscored(library: TestClient) -> None:
    """No score is not a low score: Minit is below no threshold and above none."""

    assert listed(library, metacritic_min=92) == ["Celeste", "Hades", "Portal 2"]
    assert listed(library, metacritic_max=89) == ["Dead Cells"]
    assert listed(library, metacritic_min=0) == [
        "Celeste",
        "Dead Cells",
        "Hades",
        "Portal 2",
    ]


def test_a_year_range_is_inclusive(library: TestClient) -> None:
    assert listed(library, year_min=2017, year_max=2018) == [
        "Celeste",
        "Dead Cells",
        "Hollow Knight: Soundtrack",
    ]


def test_playtime_reads_a_missing_state_row_as_the_zero_the_card_shows(
    library: TestClient,
) -> None:
    unplayed = listed(library, playtime_max=0)
    assert "Minit" in unplayed
    assert listed(library, playtime_min=600) == ["Celeste", "Hades"]


async def test_status_reads_a_missing_state_row_as_not_started(
    library: TestClient, session: AsyncSession
) -> None:
    hades = await session.scalar(select(Work.id).where(Work.title == "Hades"))
    library.patch(f"/api/works/{hades}/state", json={"play_status": "playing"})

    assert listed(library, status="playing") == ["Hades"]
    not_started = listed(library, status="not_started")
    assert "Minit" in not_started and "Hades" not in not_started


async def test_hidden_works_leave_the_listing_but_not_the_library(
    library: TestClient, session: AsyncSession
) -> None:
    hades = await session.scalar(select(Work.id).where(Work.title == "Hades"))
    library.patch(f"/api/works/{hades}/state", json={"is_hidden": True})

    assert "Hades" not in listed(library)
    assert "Hades" in listed(library, hidden="include")
    assert listed(library, hidden="only") == ["Hades"]
    assert library.get(f"/api/works/{hades}").status_code == 200


def test_filters_compose_with_each_other_and_with_search(library: TestClient) -> None:
    assert listed(library, platform="steam", year_min=2018) == ["Celeste", "Hades"]
    assert listed(library, platform="steam", year_min=2018, q="hade") == ["Hades"]


def test_a_filtered_listing_pages_on_the_same_cursor(library: TestClient) -> None:
    whole = listed(library, platform="steam")
    first = library.get("/api/works", params={"platform": "steam", "limit": 2}).json()
    second = library.get(
        "/api/works", params={"platform": "steam", "limit": 2, "cursor": first["next_cursor"]}
    ).json()

    assert [work["title"] for work in first["works"] + second["works"]] == whole[:4]


def test_a_steam_range_counts_only_a_score_steam_gave_a_verdict_on(
    library: TestClient,
) -> None:
    """The soundtrack's 100% is of too few reviews for a verdict: below no bound, above none."""

    assert listed(library, steam_min=95) == ["Celeste", "Hades", "Portal 2"]
    assert listed(library, steam_max=95) == ["Dead Cells"]
    assert listed(library, steam_min=0) == ["Celeste", "Dead Cells", "Hades", "Portal 2"]
    assert listed(library, steam_min=93, steam_max=97) == ["Celeste", "Dead Cells"]


@pytest.mark.parametrize(
    "params",
    [
        {"year_min": 2020, "year_max": 2010},
        {"metacritic_min": 101},
        {"steam_min": 101},
        {"steam_max": -1},
        {"steam_min": 90, "steam_max": 80},
        {"playtime_min": -1},
        {"kind": "not-a-kind"},
        {"platform": ["steam"] * 33},
        # Past SQLite's INTEGER: validated, then a 500 from the driver.
        {"playtime_min": 10**20},
        {"playtime_max": 10**20},
        {"platform": ""},
        {"platform": "not-a-platform"},
    ],
)
def test_a_filter_that_cannot_mean_anything_is_refused(
    library: TestClient, params: dict[str, Any]
) -> None:
    """An empty page for an impossible range would read as a claim about the library."""

    assert library.get("/api/works", params=params).status_code == 422


def test_every_filter_declares_exactly_one_predicate() -> None:
    """The registry's one promise: nothing reaches the schema without SQL behind it."""

    for name, field in LibraryFilters.model_fields.items():
        predicates = [item for item in field.metadata if isinstance(item, Predicate)]
        assert len(predicates) == 1, name


async def test_another_users_copy_does_not_put_a_work_on_a_platform(
    library: TestClient, session: AsyncSession
) -> None:
    """The platform filter uses `owned_by`, which is where the user is checked."""

    hades = await session.scalar(select(Work).where(Work.title == "Hades"))
    epic = await session.scalar(
        select(Account).where(Account.external_account_id == "0123456789abcdef")
    )
    assert hades is not None and epic is not None

    stranger = await make_user(session, "someone-else")
    entitlement = Entitlement(
        user_id=stranger.id,
        account_id=epic.id,
        provider_item_id="epic-stranger",
        provider_title="Hades",
    )
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=hades.id))
    await session.commit()

    assert "Hades" not in listed(library, platform="epic")
