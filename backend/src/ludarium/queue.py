"""The play queue: the works a user chose to play next, in the order they chose.

A queued work holds a `queue_position`, and the positions of one user's queue
are 1..n with no gaps. Every change that could open a gap or a tie ends in
`arrange`, which writes the whole queue again.

The positions are unique per user, and SQLite checks that row by row inside an
`UPDATE`, so a renumber that shifted rows in place would collide with itself
halfway through. `_write` first lifts the queue clear of 1..n and then sets
each row to its final place.
"""

from collections.abc import Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.models import UserWorkState


async def queue_of(session: AsyncSession, user_id: int) -> list[UserWorkState]:
    return list(
        await session.scalars(
            select(UserWorkState)
            .where(UserWorkState.user_id == user_id, UserWorkState.queue_position.is_not(None))
            .order_by(UserWorkState.queue_position, UserWorkState.work_id)
        )
    )


async def free_position(session: AsyncSession, user_id: int) -> int:
    """A position past every one in use: the end of the queue, or a place to park a row.

    Read before the row is changed, so the autoflush the query triggers never
    writes a queued status without its position.
    """

    highest = await session.scalar(
        select(func.max(UserWorkState.queue_position)).where(UserWorkState.user_id == user_id)
    )
    return (highest or 0) + 1


async def arrange(
    session: AsyncSession, user_id: int, placed: Mapping[int, int] | None = None
) -> None:
    """Number the queue 1..n again, with each work in `placed` at the position given for it.

    A position past the end puts the work last. Every other work keeps its
    order relative to the rest. With nothing placed this only closes gaps.
    """

    placed = placed or {}
    await session.flush()
    states = await queue_of(session, user_id)
    order = [state for state in states if state.work_id not in placed]
    by_work = {state.work_id: state for state in states}
    for work_id, position in sorted(placed.items(), key=lambda item: item[1]):
        order.insert(min(max(position, 1), len(order) + 1) - 1, by_work[work_id])
    await _write(session, order)


async def _write(session: AsyncSession, order: Sequence[UserWorkState]) -> None:
    # Positions are distinct and at least 1, so the highest is at least n, and
    # adding it moves every row past n: the second pass lands on free places.
    lift = max((state.queue_position or 0 for state in order), default=0)
    for state in order:
        state.queue_position = (state.queue_position or 0) + lift
    await session.flush()
    for position, state in enumerate(order, start=1):
        state.queue_position = position
    await session.flush()
