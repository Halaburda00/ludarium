from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_account, make_user
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_sync import THREE_GAMES, FakeLibrary

from ludarium.models import Account, Entitlement, EntitlementWork, UserWorkState, Work
from ludarium.models.types import utcnow
from ludarium.sync import sync_account

WITCHER = "292030"


@pytest.fixture
async def account(client: TestClient, session: AsyncSession) -> Account:
    """Three games synced, then a run that no longer lists The Witcher 3 and its 3247 minutes."""

    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    account = await make_account(session)
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    return account


async def remove_the_witcher(session: AsyncSession, account: Account) -> None:
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES[1:]))


async def the_witcher(session: AsyncSession) -> tuple[int, int]:
    """The Witcher's copy and its work."""

    row = (
        await session.execute(
            select(Entitlement.id, EntitlementWork.work_id)
            .join(EntitlementWork, EntitlementWork.entitlement_id == Entitlement.id)
            .where(Entitlement.provider_item_id == WITCHER)
        )
    ).one()
    # Ended, so the API's writes are not waiting on this read.
    await session.commit()
    return row[0], row[1]


def titles(client: TestClient) -> list[str]:
    return [work["title"] for work in client.get("/api/works").json()["works"]]


def removed(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/entitlements/removed")
    assert response.status_code == 200, response.text
    body: list[dict[str, Any]] = response.json()
    return body


async def test_the_view_lists_what_a_run_stopped_seeing_and_nothing_else(
    client: TestClient, session: AsyncSession, account: Account
) -> None:
    assert removed(client) == []

    await remove_the_witcher(session, account)

    entitlement_id, work_id = await the_witcher(session)
    stored = await session.get_one(Entitlement, entitlement_id)
    await session.commit()
    [listed] = removed(client)
    assert listed == {
        "id": entitlement_id,
        "provider": "steam",
        "provider_name": "Steam",
        "account_label": "Main",
        "provider_title": "The Witcher 3: Wild Hunt",
        "work_id": work_id,
        "work_title": "The Witcher 3: Wild Hunt",
        "removed_at": listed["removed_at"],
        "removed_by_run_id": stored.removed_by_run_id,
    }
    assert stored.removed_by_run_id is not None
    assert "The Witcher 3: Wild Hunt" not in titles(client)


async def test_a_restore_brings_the_work_back_with_its_state_and_playtime(
    client: TestClient, session: AsyncSession, account: Account
) -> None:
    """The removal never touched the state; the restore makes the copy count again."""

    entitlement_id, work_id = await the_witcher(session)
    assert client.patch(f"/api/works/{work_id}/state", json={"rating": 9}).status_code == 200
    await remove_the_witcher(session, account)
    assert client.get(f"/api/works/{work_id}").status_code == 404

    response = client.post(f"/api/entitlements/{entitlement_id}/restore")

    assert response.status_code == 204, response.text
    assert removed(client) == []
    work = client.get(f"/api/works/{work_id}").json()
    assert (work["rating"], work["playtime_minutes"]) == (9, 3247)
    assert [(copy["id"], copy["kept"]) for copy in work["entitlements"]] == [(entitlement_id, True)]


async def test_a_restored_copy_outlasts_the_runs_that_still_do_not_list_it(
    client: TestClient, session: AsyncSession, account: Account
) -> None:
    """ADR-0030: the user's word against the platform's silence, until the user lets it go."""

    entitlement_id, _ = await the_witcher(session)
    await remove_the_witcher(session, account)
    client.post(f"/api/entitlements/{entitlement_id}/restore")

    await remove_the_witcher(session, account)
    assert "The Witcher 3: Wild Hunt" in titles(client)

    assert client.delete(f"/api/entitlements/{entitlement_id}/keep").status_code == 204
    assert "The Witcher 3: Wild Hunt" in titles(client)
    await remove_the_witcher(session, account)
    assert [copy["id"] for copy in removed(client)] == [entitlement_id]


async def test_only_a_removed_copy_of_the_user_s_own_is_restored(
    client: TestClient, session: AsyncSession, account: Account
) -> None:
    entitlement_id, _ = await the_witcher(session)
    stranger = await make_user(session, "someone-else")
    other = await make_account(session, external_account_id="other")
    other.user_id = stranger.id
    session.add(
        theirs := Entitlement(
            account_id=other.id,
            user_id=stranger.id,
            provider_item_id="1",
            provider_title="X",
            removed_at=utcnow(),
        )
    )
    await session.commit()

    assert client.post(f"/api/entitlements/{entitlement_id}/restore").status_code == 409
    assert client.post(f"/api/entitlements/{theirs.id}/restore").status_code == 404
    assert client.delete(f"/api/entitlements/{theirs.id}/keep").status_code == 404
    assert client.post("/api/entitlements/404404/restore").status_code == 404
    assert removed(client) == []
    assert (await session.get_one(Entitlement, entitlement_id)).kept_at is None


async def test_restoring_counts_the_copy_towards_every_work_it_grants(
    client: TestClient, session: AsyncSession, account: Account
) -> None:
    """A bundle's copy adds to each of its works, so each total comes back."""

    entitlement_id, primary = await the_witcher(session)
    granted = Work(title="Hearts of Stone", sort_title="hearts of stone")
    session.add(granted)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement_id, work_id=granted.id, role="granted"))
    session.add(UserWorkState(user_id=account.user_id, work_id=granted.id))
    await session.commit()
    await remove_the_witcher(session, account)

    client.post(f"/api/entitlements/{entitlement_id}/restore")

    for work_id in (primary, granted.id):
        assert client.get(f"/api/works/{work_id}").json()["playtime_minutes"] == 3247


async def test_a_kept_copy_the_platform_lists_again_stays_kept(
    client: TestClient, session: AsyncSession, account: Account
) -> None:
    """ADR-0030: the platform listing it again is no reason for the user's word to lapse.

    So the page's wording must not claim the platform is silent about it.
    """

    entitlement_id, work_id = await the_witcher(session)
    await remove_the_witcher(session, account)
    client.post(f"/api/entitlements/{entitlement_id}/restore")

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    [copy] = client.get(f"/api/works/{work_id}").json()["entitlements"]
    assert copy["kept"] is True
