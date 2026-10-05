from typing import Any

import pytest
from conftest import make_account
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_filters import library  # noqa: F401 — the fixture, shared rather than copied
from test_merging import fresh, merge, stub
from test_work_state import patch, work_id

from ludarium.enums import MatchActor, PlayStatus
from ludarium.merging import delete_works, undo_merge
from ludarium.models import Account, UserWorkState, Work


@pytest.fixture
async def steam(session: AsyncSession) -> Account:
    return await make_account(session)


async def queue(session: AsyncSession, *works: Work) -> None:
    """Queue the works in this order, written straight to the rows."""

    for position, work in enumerate(works, start=1):
        state = await session.get_one(UserWorkState, (1, work.id))
        state.play_status, state.queue_position = PlayStatus.QUEUED, position
    await session.flush()


async def positions(session: AsyncSession) -> dict[int, int]:
    rows = await fresh(
        session,
        select(UserWorkState).where(UserWorkState.queue_position.is_not(None)),
    )
    return {state.work_id: state.queue_position for state in rows if state.queue_position}


def move(client: TestClient, work: int, position: int) -> dict[str, Any]:
    response = client.put(f"/api/works/{work}/queue", json={"position": position})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def in_queue(client: TestClient) -> list[str]:
    response = client.get("/api/works", params={"status": "queued", "sort": "queue"})
    assert response.status_code == 200, response.text
    return [work["title"] for work in response.json()["works"]]


async def titles(session: AsyncSession, *names: str) -> list[int]:
    return [await work_id(session, name) for name in names]


async def test_a_queued_work_joins_at_the_end(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades, celeste, minit = await titles(session, "Hades", "Celeste", "Minit")

    first = patch(library, hades, play_status="queued")
    patch(library, celeste, play_status="queued")
    # Minit has no state row, which the update creates.
    last = patch(library, minit, play_status="queued")

    assert (first["queue_position"], last["queue_position"]) == (1, 3)
    assert in_queue(library) == ["Hades", "Celeste", "Minit"]


async def test_queueing_a_queued_work_again_keeps_its_place(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades, celeste = await titles(session, "Hades", "Celeste")
    patch(library, hades, play_status="queued")
    patch(library, celeste, play_status="queued")

    assert patch(library, hades, play_status="queued", rating=8)["queue_position"] == 1


async def test_a_move_shifts_the_rest_and_past_the_end_means_last(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades, celeste, minit = await titles(session, "Hades", "Celeste", "Minit")
    for work in (hades, celeste, minit):
        patch(library, work, play_status="queued")

    assert move(library, minit, 1)["queue_position"] == 1
    assert in_queue(library) == ["Minit", "Hades", "Celeste"]
    assert move(library, minit, 99)["queue_position"] == 3
    assert in_queue(library) == ["Hades", "Celeste", "Minit"]
    assert move(library, celeste, 2)["queue_position"] == 2


async def test_leaving_the_queue_closes_the_gap(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades, celeste, minit = await titles(session, "Hades", "Celeste", "Minit")
    for work in (hades, celeste, minit):
        patch(library, work, play_status="queued")

    left = patch(library, hades, play_status="playing")

    assert (left["play_status"], left["queue_position"]) == ("playing", None)
    assert library.get(f"/api/works/{minit}").json()["queue_position"] == 2
    assert in_queue(library) == ["Celeste", "Minit"]


async def test_a_work_that_is_not_queued_cannot_be_moved(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades = await work_id(session, "Hades")

    assert library.put(f"/api/works/{hades}/queue", json={"position": 1}).status_code == 409
    assert library.put("/api/works/999999/queue", json={"position": 1}).status_code == 404
    patch(library, hades, play_status="queued")
    assert library.put(f"/api/works/{hades}/queue", json={"position": 0}).status_code == 422


async def test_the_queue_order_pages_on_its_cursor(
    library: TestClient,  # noqa: F811
    session: AsyncSession,
) -> None:
    hades, celeste, minit = await titles(session, "Hades", "Celeste", "Minit")
    for work in (minit, hades, celeste):
        patch(library, work, play_status="queued")

    seen: list[str] = []
    params: dict[str, Any] = {"status": "queued", "sort": "queue", "limit": 1}
    while True:
        page = library.get("/api/works", params=params).json()
        seen += [work["title"] for work in page["works"]]
        if page["next_cursor"] is None:
            break
        params["cursor"] = page["next_cursor"]

    assert seen == ["Minit", "Hades", "Celeste"]


@pytest.mark.parametrize(
    ("status", "position"),
    [(PlayStatus.QUEUED, None), (PlayStatus.QUEUED, 0), (PlayStatus.PLAYING, 1)],
)
async def test_the_schema_refuses_a_place_without_the_status_and_the_reverse(
    session: AsyncSession, steam: Account, status: PlayStatus, position: int | None
) -> None:
    work = await stub(session, steam, "292030", "The Witcher 3: Wild Hunt")
    state = await session.get_one(UserWorkState, (1, work.id))
    state.play_status, state.queue_position = status, position

    with pytest.raises(IntegrityError, match="queue_position_iff_queued"):
        await session.flush()


async def test_two_works_cannot_share_a_place(session: AsyncSession, steam: Account) -> None:
    witcher = await stub(session, steam, "292030", "The Witcher 3: Wild Hunt")
    hades = await stub(session, steam, "1145360", "Hades")
    await queue(session, witcher)
    state = await session.get_one(UserWorkState, (1, hades.id))
    state.play_status, state.queue_position = PlayStatus.QUEUED, 1

    with pytest.raises(IntegrityError, match="UNIQUE"):
        await session.flush()


async def test_a_merge_of_two_queued_works_keeps_the_better_place_and_the_undo_both(
    session: AsyncSession, steam: Account
) -> None:
    first = await stub(session, steam, "1", "Hades")
    target = await stub(session, steam, "292030", "The Witcher 3: Wild Hunt")
    last = await stub(session, steam, "2", "Celeste")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    target.is_matched = True
    await queue(session, first, source, last, target)

    audit = await merge(session, source, target)

    assert await positions(session) == {first.id: 1, target.id: 2, last.id: 3}

    restored = (await undo_merge(session, audit_id=audit.id, actor=MatchActor.USER)).work_id

    assert await positions(session) == {first.id: 1, restored: 2, last.id: 3, target.id: 4}


async def test_a_target_left_at_its_default_takes_the_source_s_place_and_gives_it_back(
    session: AsyncSession, steam: Account
) -> None:
    first = await stub(session, steam, "1", "Hades")
    target = await stub(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    last = await stub(session, steam, "2", "Celeste")
    await queue(session, first, source, last)

    audit = await merge(session, source, target)

    assert await positions(session) == {first.id: 1, target.id: 2, last.id: 3}

    restored = (await undo_merge(session, audit_id=audit.id, actor=MatchActor.USER)).work_id

    assert await positions(session) == {first.id: 1, restored: 2, last.id: 3}
    state = await session.get_one(UserWorkState, (1, target.id), populate_existing=True)
    assert state.play_status is PlayStatus.NOT_STARTED


async def test_a_target_the_user_is_playing_stays_out_of_the_queue(
    session: AsyncSession, steam: Account
) -> None:
    first = await stub(session, steam, "1", "Hades")
    target = await stub(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    last = await stub(session, steam, "2", "Celeste")
    await queue(session, first, source, last)
    (await session.get_one(UserWorkState, (1, target.id))).play_status = PlayStatus.PLAYING

    audit = await merge(session, source, target)

    assert await positions(session) == {first.id: 1, last.id: 2}

    restored = (await undo_merge(session, audit_id=audit.id, actor=MatchActor.USER)).work_id

    assert await positions(session) == {first.id: 1, restored: 2, last.id: 3}


async def test_an_undo_after_the_user_took_the_target_out_of_the_queue(
    session: AsyncSession, steam: Account
) -> None:
    target = await stub(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    last = await stub(session, steam, "2", "Celeste")
    target.is_matched = True
    await queue(session, source, target, last)
    audit = await merge(session, source, target)
    state = await session.get_one(UserWorkState, (1, target.id))
    state.play_status, state.queue_position = PlayStatus.PLAYING, None
    await session.flush()

    restored = (await undo_merge(session, audit_id=audit.id, actor=MatchActor.USER)).work_id

    assert await positions(session) == {restored: 1, last.id: 2}


async def test_a_deleted_work_leaves_the_queue_and_the_gap_closes(
    session: AsyncSession, steam: Account
) -> None:
    first = await stub(session, steam, "1", "Hades")
    gone = await stub(session, steam, None, "A disc on the shelf")
    last = await stub(session, steam, "2", "Celeste")
    await queue(session, first, gone, last)

    await delete_works(session, [gone.id])

    assert await positions(session) == {first.id: 1, last.id: 2}
