"""Add-ons folded under their game in the listing, and listed on its page (#98)."""

from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_account
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_sync import FakeLibrary, civ_and_rise, owned

from ludarium.enums import ItemKind
from ludarium.models import Account, Work
from ludarium.sync import sync_account

# An add-on whose game this account does not own: Epic names one all the same.
LONELY = owned(
    "bd0bf1b9a1c94a5b9d2f8b7b3d4f6a10",
    "Train Sim World 3: Bakerloo Line",
    item_kind=ItemKind.DLC,
    parent_item_id="0f0e0d0c0b0a09080706050403020100",
)


@pytest.fixture
async def epic(client: TestClient, session: AsyncSession) -> Account:
    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    account = await make_account(session, key="epic", external_account_id="0123456789abcdef")
    await sync_account(
        session, account=account, library=FakeLibrary([*civ_and_rise(), LONELY], key="epic")
    )
    return account


def listed(client: TestClient, query: str = "") -> dict[str, int]:
    """Each card's title and how many add-ons it counts."""

    response = client.get(f"/api/works{query}")
    assert response.status_code == 200, response.text
    works: list[dict[str, Any]] = response.json()["works"]
    return {work["title"]: work["addon_count"] for work in works}


async def work_id(session: AsyncSession, title: str) -> int:
    found = await session.scalar(select(Work.id).where(Work.title == title))
    await session.commit()
    assert found is not None
    return found


async def test_an_add_on_is_listed_through_its_game(client: TestClient, epic: Account) -> None:
    assert listed(client) == {
        "Sid Meier's Civilization VI": 1,
        # Its game is not owned, so there is no card to fold it under.
        "Train Sim World 3: Bakerloo Line": 0,
    }


async def test_add_ons_can_be_listed_on_their_own_again(client: TestClient, epic: Account) -> None:
    assert listed(client, "?addons=separate") == {
        "Rise and Fall": 0,
        "Sid Meier's Civilization VI": 1,
        "Train Sim World 3: Bakerloo Line": 0,
    }


async def test_a_search_for_a_folded_add_on_finds_its_game(
    client: TestClient, epic: Account
) -> None:
    """Otherwise a folded add-on could not be found at all."""

    assert listed(client, "?q=rise") == {"Sid Meier's Civilization VI": 1}
    assert listed(client, "?q=rise&addons=separate") == {"Rise and Fall": 0}


async def test_a_kind_looks_through_to_folded_add_ons(client: TestClient, epic: Account) -> None:
    assert listed(client, "?kind=dlc") == {
        "Sid Meier's Civilization VI": 1,
        "Train Sim World 3: Bakerloo Line": 0,
    }
    assert listed(client, "?kind=dlc&addons=separate") == {
        "Rise and Fall": 0,
        "Train Sim World 3: Bakerloo Line": 0,
    }


async def test_the_other_filters_describe_the_game_itself(
    client: TestClient, session: AsyncSession, epic: Account
) -> None:
    """ADR-0032: an add-on from 2018 does not make its game a 2018 game."""

    rise = await session.get_one(Work, await work_id(session, "Rise and Fall"))
    rise.release_year = 2018
    await session.commit()

    assert listed(client, "?year_min=2018") == {}
    assert listed(client, "?year_min=2018&addons=separate") == {"Rise and Fall": 0}


async def test_an_add_on_whose_game_was_removed_is_a_card_again(
    client: TestClient, session: AsyncSession, epic: Account
) -> None:
    await sync_account(
        session,
        account=epic,
        library=FakeLibrary([*civ_and_rise(with_civ=False), LONELY], key="epic"),
    )

    assert listed(client) == {
        "Rise and Fall": 0,
        "Train Sim World 3: Bakerloo Line": 0,
    }


async def test_a_game_s_page_lists_its_add_ons_and_an_add_on_s_names_its_game(
    client: TestClient, session: AsyncSession, epic: Account
) -> None:
    civ, rise = (
        await work_id(session, "Sid Meier's Civilization VI"),
        await work_id(session, "Rise and Fall"),
    )

    game = client.get(f"/api/works/{civ}").json()
    addon = client.get(f"/api/works/{rise}").json()

    assert (game["addons"], game["parent"]) == (
        [{"id": rise, "title": "Rise and Fall", "item_kind": "dlc"}],
        None,
    )
    assert (addon["addons"], addon["parent"]) == (
        [],
        {"id": civ, "title": "Sid Meier's Civilization VI", "item_kind": "game"},
    )


async def test_an_unknown_choice_is_refused(client: TestClient, epic: Account) -> None:
    assert client.get("/api/works?addons=hide").status_code == 422


async def test_a_removed_add_on_does_not_bring_its_game_into_a_search(
    client: TestClient, session: AsyncSession, epic: Account
) -> None:
    """Only owned add-ons are looked through, as only owned copies put a work in the list."""

    civ = [item for item in civ_and_rise() if item.parent_item_id is None]
    await sync_account(session, account=epic, library=FakeLibrary([*civ, LONELY], key="epic"))

    assert listed(client, "?q=rise") == {}
    assert listed(client) == {
        "Sid Meier's Civilization VI": 0,
        "Train Sim World 3: Bakerloo Line": 0,
    }


async def test_hiding_a_game_does_not_hide_its_add_ons_with_it(
    client: TestClient, session: AsyncSession, epic: Account
) -> None:
    """A hidden game has no card in the default view, so there is nothing to fold under."""

    civ = await work_id(session, "Sid Meier's Civilization VI")
    assert client.patch(f"/api/works/{civ}/state", json={"is_hidden": True}).status_code == 200

    assert listed(client) == {"Rise and Fall": 0, "Train Sim World 3: Bakerloo Line": 0}
    assert listed(client, "?hidden=only") == {"Sid Meier's Civilization VI": 1}


async def test_a_hidden_add_on_of_a_shown_game_is_in_the_hidden_view(
    client: TestClient, session: AsyncSession, epic: Account
) -> None:
    """The hidden view is the one place a hidden work is un-hidden from."""

    rise = await work_id(session, "Rise and Fall")
    assert client.patch(f"/api/works/{rise}/state", json={"is_hidden": True}).status_code == 200

    assert listed(client, "?hidden=only") == {"Rise and Fall": 0}
    assert listed(client, "?hidden=include") == {
        "Sid Meier's Civilization VI": 1,
        "Train Sim World 3: Bakerloo Line": 0,
    }
