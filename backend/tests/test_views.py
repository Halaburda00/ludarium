from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_user
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.models import SavedView


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    return client


def save(client: TestClient, name: str, query: str = "") -> dict[str, Any]:
    response = client.post("/api/views", json={"name": name, "query": query})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def names(client: TestClient) -> list[str]:
    return [view["name"] for view in client.get("/api/views").json()]


def test_a_view_is_saved_in_the_form_it_is_read_back_in(signed_in: TestClient) -> None:
    """Declaration order, defaults left out: `hidden=exclude` and `order=asc` say nothing."""

    view = save(
        signed_in,
        "Unplayed and well-rated",
        "sort=metacritic&order=desc&status=not_started&metacritic_min=80&hidden=exclude",
    )

    assert view["query"] == "metacritic_min=80&status=not_started&sort=metacritic&order=desc"
    assert view["dropped"] == []
    assert signed_in.get("/api/views").json() == [view]


def test_a_saved_query_is_one_the_listing_takes(signed_in: TestClient) -> None:
    view = save(signed_in, "Short games", "playtime_max=300&kind=game&sort=playtime")

    assert signed_in.get(f"/api/works?{view['query']}").status_code == 200


def test_views_are_listed_in_the_order_they_were_saved(signed_in: TestClient) -> None:
    for name in ("Backlog", "Short games", "Played this year"):
        save(signed_in, name)

    assert names(signed_in) == ["Backlog", "Short games", "Played this year"]


def test_a_view_of_the_whole_library_is_a_view(signed_in: TestClient) -> None:
    assert save(signed_in, "Everything")["query"] == ""


@pytest.mark.parametrize(
    "query",
    [
        "q=hades",
        "cursor=abc",
        "limit=10",
        "genre=rpg",
        "kind=spaceship",
        "sort=rating",
        "platform=gog",
        "year_min=2020&year_max=2010",
    ],
)
def test_saving_what_the_listing_would_not_take_is_refused(
    signed_in: TestClient, query: str
) -> None:
    """Dropped quietly, the view saved would not be the one on the screen."""

    response = signed_in.post("/api/views", json={"name": "Broken", "query": query})

    assert response.status_code == 422
    assert names(signed_in) == []


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_a_name_is_a_short_label(signed_in: TestClient, name: str) -> None:
    assert signed_in.post("/api/views", json={"name": name, "query": ""}).status_code == 422


def test_a_name_is_trimmed(signed_in: TestClient) -> None:
    assert save(signed_in, "  Backlog  ")["name"] == "Backlog"


def test_two_views_cannot_share_a_name(signed_in: TestClient) -> None:
    save(signed_in, "Backlog")

    response = signed_in.post("/api/views", json={"name": "Backlog", "query": "kind=dlc"})

    assert response.status_code == 409
    assert names(signed_in) == ["Backlog"]


async def test_a_view_that_names_what_no_longer_exists_opens_without_it(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """Saved when all of it was valid; each stale part goes on its own, and is named."""

    session.add(
        SavedView(
            user_id=1,
            name="From an older version",
            query=(
                "platform=steam&platform=gog&kind=dlc&kind=spaceship&genre=rpg"
                "&metacritic_min=90&metacritic_max=80&year_min=2010&sort=rating"
            ),
        )
    )
    await session.commit()

    [view] = signed_in.get("/api/views").json()

    assert view["query"] == "platform=steam&kind=dlc&year_min=2010"
    assert sorted(view["dropped"]) == [
        "genre=rpg",
        "kind=spaceship",
        "metacritic_max=80",
        "metacritic_min=90",
        "platform=gog",
        "sort=rating",
    ]


async def test_a_value_given_twice_where_one_is_expected_is_dropped(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """Neither value is the one the view meant, so neither is picked."""

    session.add(SavedView(user_id=1, name="Twice", query="sort=playtime&sort=metacritic"))
    await session.commit()

    [view] = signed_in.get("/api/views").json()

    assert view["query"] == ""
    assert view["dropped"] == ["sort=playtime", "sort=metacritic"]


def test_a_view_can_be_renamed(signed_in: TestClient) -> None:
    view = save(signed_in, "Backlog", "status=not_started")

    response = signed_in.patch(f"/api/views/{view['id']}", json={"name": "Pile of shame"})

    assert response.status_code == 200
    assert response.json() == {**view, "name": "Pile of shame"}
    assert names(signed_in) == ["Pile of shame"]


def test_a_rename_onto_another_view_s_name_is_refused(signed_in: TestClient) -> None:
    save(signed_in, "Backlog")
    other = save(signed_in, "Short games")

    response = signed_in.patch(f"/api/views/{other['id']}", json={"name": "Backlog"})

    assert response.status_code == 409
    assert names(signed_in) == ["Backlog", "Short games"]


def test_views_can_be_reordered(signed_in: TestClient) -> None:
    ids = [save(signed_in, name)["id"] for name in ("A", "B", "C")]

    response = signed_in.put("/api/views/order", json={"ids": [ids[2], ids[0], ids[1]]})

    assert response.status_code == 200
    assert [view["name"] for view in response.json()] == ["C", "A", "B"]
    assert names(signed_in) == ["C", "A", "B"]


def test_a_view_saved_after_a_reorder_goes_last(signed_in: TestClient) -> None:
    ids = [save(signed_in, name)["id"] for name in ("A", "B")]
    signed_in.put("/api/views/order", json={"ids": ids[::-1]})

    save(signed_in, "C")

    assert names(signed_in) == ["B", "A", "C"]


@pytest.mark.parametrize(
    "pick", [lambda ids: ids[:2], lambda ids: [*ids, ids[0]], lambda ids: [*ids, 999]]
)
def test_an_order_that_is_not_every_view_once_is_refused(signed_in: TestClient, pick: Any) -> None:
    ids = [save(signed_in, name)["id"] for name in ("A", "B", "C")]

    response = signed_in.put("/api/views/order", json={"ids": pick(ids)})

    assert response.status_code == 409
    assert names(signed_in) == ["A", "B", "C"]


def test_a_view_can_be_deleted(signed_in: TestClient) -> None:
    view = save(signed_in, "Backlog")

    assert signed_in.delete(f"/api/views/{view['id']}").status_code == 204
    assert names(signed_in) == []
    assert signed_in.delete(f"/api/views/{view['id']}").status_code == 404


async def test_another_user_s_views_are_not_theirs_to_see_or_change(
    signed_in: TestClient, session: AsyncSession
) -> None:
    other = await make_user(session, "someone-else")
    theirs = SavedView(user_id=other.id, name="Theirs", query="kind=dlc")
    session.add(theirs)
    await session.commit()
    mine = save(signed_in, "Mine")

    assert names(signed_in) == ["Mine"]
    assert signed_in.patch(f"/api/views/{theirs.id}", json={"name": "Taken"}).status_code == 404
    assert signed_in.delete(f"/api/views/{theirs.id}").status_code == 404
    assert (
        signed_in.put("/api/views/order", json={"ids": [mine["id"], theirs.id]}).status_code == 409
    )
    # And their name is not taken from this user.
    assert save(signed_in, "Theirs")["name"] == "Theirs"


async def test_a_value_repeated_past_the_list_bound_is_read_once(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """Forty `kind=game` are one kind, not forty: counted as forty they overflow the bound."""

    repeated = "&".join(["kind=game"] * 40)

    saved = signed_in.post("/api/views", json={"name": "Repeated", "query": repeated})
    session.add(SavedView(user_id=1, name="Stored", query=repeated))
    await session.commit()

    assert saved.status_code == 201
    assert saved.json()["query"] == "kind=game"
    assert [view["query"] for view in signed_in.get("/api/views").json()] == [
        "kind=game",
        "kind=game",
    ]


def test_a_view_keeps_a_steam_threshold_only_when_it_is_not_the_default(
    signed_in: TestClient,
) -> None:
    assert (
        save(signed_in, "Default", "steam_min=90&steam_reviews_min=10")["query"] == "steam_min=90"
    )
    assert (
        save(signed_in, "Small games", "steam_min=90&steam_reviews_min=1")["query"]
        == "steam_min=90&steam_reviews_min=1"
    )
    assert (
        signed_in.post(
            "/api/views", json={"name": "Zero", "query": "steam_reviews_min=0"}
        ).status_code
        == 422
    )
