import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_user
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_filters import LIBRARY, Game, seed

from ludarium.models import Account, Entitlement, EntitlementWork, Genre, Work, WorkGenre

# Owned only through a removed copy: out of the library, so its genre is too.
GONE = Game("Gone Home", ("steam",), removed_on=("steam",), genres=("strategy",))


@pytest.fixture
async def library(client: TestClient, session: AsyncSession) -> TestClient:
    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    await seed(session, (*LIBRARY, GONE))
    return client


def test_the_panel_is_offered_the_genres_in_the_library_by_name(library: TestClient) -> None:
    assert library.get("/api/genres").json() == [
        {"slug": "platformer", "name": "Platformer"},
        {"slug": "puzzle", "name": "Puzzle"},
        {"slug": "roguelike", "name": "Roguelike"},
    ]


async def test_another_user_s_games_add_no_genres(
    library: TestClient, session: AsyncSession
) -> None:
    stranger = await make_user(session, "someone-else")
    account = await session.scalar(
        select(Account).where(Account.external_account_id == "765611979")
    )
    assert account is not None
    work = Work(title="Theirs", sort_title="Theirs")
    genre = Genre(slug="racing", name="Racing")
    session.add_all([work, genre])
    await session.flush()
    entitlement = Entitlement(
        user_id=stranger.id, account_id=account.id, provider_item_id="x", provider_title="Theirs"
    )
    session.add(entitlement)
    await session.flush()
    session.add_all(
        [
            EntitlementWork(entitlement_id=entitlement.id, work_id=work.id),
            WorkGenre(work_id=work.id, genre_id=genre.id, source_ref="igdb"),
        ]
    )
    await session.commit()

    assert "racing" not in [genre["slug"] for genre in library.get("/api/genres").json()]


async def test_a_work_s_page_names_its_genres(library: TestClient, session: AsyncSession) -> None:
    dead_cells = await session.scalar(select(Work.id).where(Work.title == "Dead Cells"))
    minit = await session.scalar(select(Work.id).where(Work.title == "Minit"))

    assert library.get(f"/api/works/{dead_cells}").json()["genres"] == [
        {"slug": "platformer", "name": "Platformer"},
        {"slug": "roguelike", "name": "Roguelike"},
    ]
    assert library.get(f"/api/works/{minit}").json()["genres"] == []


def test_the_genres_need_a_session(client: TestClient) -> None:
    assert client.get("/api/genres").status_code == 401
