from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_filters import library  # noqa: F401 — the fixture, shared rather than copied

from ludarium.enums import PlayStatus
from ludarium.models import UserWorkState, Work
from ludarium.resolver import resolve_work_aggregates_many


async def work_id(session: AsyncSession, title: str) -> int:
    found = await session.scalar(select(Work.id).where(Work.title == title))
    assert found is not None
    return found


def patch(client: TestClient, work: int, **body: Any) -> dict[str, Any]:
    response = client.patch(f"/api/works/{work}/state", json=body)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_the_answer_is_the_work_as_it_now_is(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades = await work_id(session, "Hades")

    body = patch(
        library, hades, play_status="playing", rating=9, notes="Asphodel", is_favourite=True
    )

    assert (body["play_status"], body["rating"], body["notes"]) == ("playing", 9, "Asphodel")
    assert body["is_favourite"] is True
    assert library.get(f"/api/works/{hades}").json()["rating"] == 9


async def test_a_field_left_out_is_left_alone_and_a_null_clears_it(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades = await work_id(session, "Hades")
    patch(library, hades, rating=9, notes="Asphodel")

    body = patch(library, hades, rating=None)

    assert (body["rating"], body["notes"]) == (None, "Asphodel")


async def test_blank_notes_are_no_notes(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades = await work_id(session, "Hades")

    assert patch(library, hades, notes="   \n")["notes"] is None


async def test_starting_and_finishing_are_dated_once_and_never_undated(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    """ "Finished it in 2024" stays true after a replay moves it back to playing."""

    hades = await work_id(session, "Hades")

    started = patch(library, hades, play_status="playing")["started_at"]
    finished = patch(library, hades, play_status="completed")
    again = patch(library, hades, play_status="playing")

    assert started is not None and finished["completed_at"] is not None
    assert finished["started_at"] == started
    assert (again["started_at"], again["completed_at"]) == (started, finished["completed_at"])


async def test_only_a_change_of_status_dates_anything(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    """A rating given today is not the day the game was started."""

    hades = await work_id(session, "Hades")
    state = await session.get(UserWorkState, (1, hades))
    assert state is not None
    # Playing with no date: how a row carried over by a merge, or set before
    # this endpoint existed, can look.
    state.play_status = PlayStatus.PLAYING
    await session.commit()

    assert patch(library, hades, rating=7)["started_at"] is None


async def test_a_work_with_no_state_row_gets_one(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    """Minit was seeded without one, which nothing in the schema prevents."""

    minit = await work_id(session, "Minit")

    body = patch(library, minit, is_hidden=True)

    assert body["is_hidden"] is True
    assert body["play_status"] == "not_started"
    # Out of the read this session opened before the request committed.
    await session.rollback()
    assert await session.get(UserWorkState, (1, minit)) is not None


async def test_a_work_outside_the_library_is_a_404(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    missing = library.patch("/api/works/999999/state", json={"rating": 5})

    assert missing.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rating": 0},
        {"rating": 11},
        {"notes": "x" * 10_001},
        {"play_status": None},
        {"play_status": "abandoned"},
        {"is_hidden": None},
        {"title": "Not yours to change here"},
    ],
)
async def test_an_update_that_cannot_mean_anything_is_refused(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
    body: dict[str, Any],
) -> None:
    hades = await work_id(session, "Hades")

    assert library.patch(f"/api/works/{hades}/state", json=body).status_code == 422


async def test_a_later_sync_leaves_the_user_s_state_alone(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    """Rule 3: the aggregates a sync recomputes share the row, and only their columns move."""

    hades = await work_id(session, "Hades")
    patch(library, hades, play_status="completed", rating=10, is_hidden=True)
    await session.rollback()

    await resolve_work_aggregates_many(session, work_ids=[hades], user_id=1)
    await session.commit()

    body = library.get(f"/api/works/{hades}").json()
    assert (body["play_status"], body["rating"], body["is_hidden"]) == ("completed", 10, True)


def test_changing_state_needs_a_session(client: TestClient) -> None:
    assert client.patch("/api/works/1/state", json={"rating": 5}).status_code == 401
