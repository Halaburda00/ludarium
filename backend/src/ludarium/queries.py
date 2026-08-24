"""Query fragments more than one place needs to get right.

`user_id` and `removed_at` are the two the codebase keeps retyping, and the two
whose omission is silent: forget the first and one account's library leaks into
another's, forget the second and rule 1's marked-not-deleted rows come back from
the dead. Mutation testing found the pair missing from three separate endpoints
before this existed, each time because a single-user fixture cannot tell a
scoped query from an unscoped one.

`test_no_query_writes_the_ownership_predicate_by_hand` is what keeps this a
mechanism rather than a habit.
"""

from collections.abc import Iterator, Sequence
from typing import Final

from sqlalchemy import ColumnElement

from ludarium.models import Entitlement

# How many ids to name in one `IN (...)`. SQLite's ceiling on bound parameters
# is 32766 on anything current and 999 on builds older than 3.32; PostgreSQL
# stops at 65535. Well under all three, because the cost of being wrong is not a
# slow query but a run that raises inside its own transaction and rolls the
# whole sync back — which for a large library would happen every time.
BIND_LIMIT: Final = 900


def owned_by(user_id: int) -> tuple[ColumnElement[bool], ...]:
    """What "a copy this user still has" means, in one place.

    Spread with `*` into a `where`, so that adding a third condition later
    reaches every caller at once.
    """

    return (Entitlement.user_id == user_id, Entitlement.removed_at.is_(None))


def in_batches[T](values: Sequence[T]) -> Iterator[Sequence[T]]:
    """One `IN (...)` list at a time, so a large library is more queries, not an error.

    Yields nothing for an empty sequence, which is the caller's cue that there
    is no query to run rather than one matching everything.

    The limit is read here rather than bound as a default, so a test can lower
    it to two and exercise the second batch without building a library of a
    thousand games to do it.
    """

    for start in range(0, len(values), BIND_LIMIT):
        yield values[start : start + BIND_LIMIT]
