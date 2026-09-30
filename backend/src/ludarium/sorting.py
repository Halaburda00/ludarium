"""The orders the library can be listed in, and the keyset each one pages on.

Every order but the title is a value followed by the title: works with the same
score are listed alphabetically, not in the order they were synced. That puts
three things in the keyset — `(value, sort_key, id)` — and the cursor carries
all three. Two works tied on value are common rather than rare: most of a
library has never been played, and a page boundary falling inside those
hundreds must still land between two of them.

The title breaks ties A to Z in both directions. "Best-rated first" reversed
the whole key would list the 85s from Z to A, which reads as a bug. So the
value's direction and the tie's are separate, and the keyset is written out
rather than a row-value comparison, which can only compare every column the
same way.

A null is not known, and is listed after every value in both directions: a
work with no score is not the worst-rated one, nor the best. SQLite orders
nulls first ascending and PostgreSQL orders them first descending, so neither
default is used; `value IS NULL` leads the key on both engines.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, and_, literal, or_, tuple_
from sqlalchemy.orm import InstrumentedAttribute

from ludarium.models import UserWorkState, Work
from ludarium.scores import DEFAULT_STEAM_REVIEWS, steam_score, steam_score_of

type SortValue = int | date | datetime | None

# What a 64-bit integer column, and so the bind, can hold. A cursor is ours,
# but it arrives from outside: past this the driver raises at execution and a
# made-up cursor is a 500 rather than a 400.
INTEGERS = range(-(2**63), 2**63)


class Sort(StrEnum):
    TITLE = "title"
    METACRITIC = "metacritic"
    STEAM_REVIEWS = "steam_reviews"
    PLAYTIME = "playtime"
    LAST_PLAYED = "last_played"
    RELEASE_DATE = "release_date"


class Direction(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class Key:
    """What an order compares: the expression in SQL, and the same value read off a row.

    Both take the Steam review threshold, which only the Steam order reads.
    """

    expression: Callable[[int], ColumnElement[Any]]
    read: Callable[[Work, UserWorkState | None, int], SortValue]
    # Whether `work` carries an index this order seeks on (`models.catalogue`),
    # at the default threshold.
    indexed: bool


def _on_work(column: InstrumentedAttribute[Any]) -> Key:
    return Key(
        lambda _: column.expression,
        lambda work, _, __: getattr(work, column.key),
        indexed=True,
    )


def _on_state(column: InstrumentedAttribute[Any]) -> Key:
    return Key(
        lambda _: column.expression,
        lambda _, state, __: getattr(state, column.key) if state is not None else None,
        indexed=False,
    )


# The title order has no value of its own: its key is the tie-break.
#
# Playtime and last played are on `user_work_state`, which the listing
# outer-joins, so a work with no state row sorts as null. The card shows it
# with 0 minutes, which is the default it would get, not a measurement.
KEYS: dict[Sort, Key] = {
    Sort.METACRITIC: _on_work(Work.metacritic_score),
    # Over at least the user's threshold of reviews, as the Steam range counts
    # it (`ludarium.scores`): under it a score sorts with the unscored.
    Sort.STEAM_REVIEWS: Key(
        steam_score, lambda work, _, threshold: steam_score_of(work, threshold), indexed=True
    ),
    Sort.PLAYTIME: _on_state(UserWorkState.playtime_minutes),
    Sort.LAST_PLAYED: _on_state(UserWorkState.last_played_at),
    Sort.RELEASE_DATE: _on_work(Work.release_date),
}


@dataclass(frozen=True, slots=True)
class Ordering:
    sort: Sort
    direction: Direction
    steam_reviews: int = DEFAULT_STEAM_REVIEWS

    @property
    def _column(self) -> ColumnElement[Any] | None:
        key = KEYS.get(self.sort)
        return key.expression(self.steam_reviews) if key is not None else None

    def order_by(self) -> tuple[ColumnElement[Any], ...]:
        column = self._column
        if column is None:
            if self.direction is Direction.DESC:
                return Work.sort_key.desc(), Work.id.desc()
            return Work.sort_key.asc(), Work.id.asc()
        value = column.desc() if self.direction is Direction.DESC else column.asc()
        return column.is_(None), value, Work.sort_key.asc(), Work.id.asc()

    def after(self, value: SortValue, key: str, work_id: int) -> ColumnElement[bool]:
        """Every row past `(value, key, work_id)` in this order."""

        position = tuple_(literal(key), literal(work_id))
        tie = tuple_(Work.sort_key, Work.id) > position
        column = self._column
        if column is None:
            return (
                tie
                if self.direction is Direction.ASC
                else tuple_(Work.sort_key, Work.id) < position
            )
        if value is None:
            # Past the last value already: only the rest of the nulls remain.
            return and_(column.is_(None), tie)
        beyond = column > value if self.direction is Direction.ASC else column < value
        return or_(column.is_(None), beyond, and_(column == value, tie))

    def value_of(self, work: Work, state: UserWorkState | None) -> SortValue:
        key = KEYS.get(self.sort)
        return key.read(work, state, self.steam_reviews) if key is not None else None

    def encode(self, value: SortValue) -> int | str | None:
        return value.isoformat() if isinstance(value, date) else value

    def decode(self, raw: object) -> SortValue:
        """A value read back from a cursor, checked against what this order compares.

        Raises `ValueError` for anything else. A title-ordered cursor carries no
        value; a date must parse as one; a moment must say it is UTC, because a
        naive one is refused at the bind, which would be a 500 rather than a 400.
        """

        if raw is None:
            return None
        try:
            return self._decode(raw)
        except OverflowError as exc:
            raise ValueError(f"not a {self.sort} value") from exc

    def _decode(self, raw: object) -> SortValue:
        match self.sort:
            case Sort.TITLE:
                pass
            case Sort.LAST_PLAYED if isinstance(raw, str):
                moment = datetime.fromisoformat(raw)
                # Converted here rather than at the bind, which is where an
                # offset that carries it past year 1 or 9999 would overflow.
                if moment.tzinfo is not None:
                    return moment.astimezone(UTC)
            case Sort.RELEASE_DATE if isinstance(raw, str):
                return date.fromisoformat(raw)
            case Sort.METACRITIC | Sort.STEAM_REVIEWS | Sort.PLAYTIME if (
                isinstance(raw, int) and not isinstance(raw, bool) and raw in INTEGERS
            ):
                return raw
        raise ValueError(f"not a {self.sort} value")
