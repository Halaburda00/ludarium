"""Predicates that more than one query needs to get right.

`user_id` and `removed_at` are the two the codebase keeps retyping, and the two
whose omission is silent: forget the first and one account's library leaks into
another's, forget the second and rule 1's marked-not-deleted rows come back from
the dead. Mutation testing found the pair missing from three separate endpoints
before this existed, each time because a single-user fixture cannot tell a
scoped query from an unscoped one.

`test_no_query_writes_the_ownership_predicate_by_hand` is what keeps this a
mechanism rather than a habit.
"""

from sqlalchemy import ColumnElement

from ludarium.models import Entitlement


def owned_by(user_id: int) -> tuple[ColumnElement[bool], ...]:
    """What "a copy this user still has" means, in one place.

    Spread with `*` into a `where`, so that adding a third condition later
    reaches every caller at once.
    """

    return (Entitlement.user_id == user_id, Entitlement.removed_at.is_(None))
