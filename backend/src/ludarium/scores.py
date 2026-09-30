"""When a Steam score counts: over at least as many reviews as the user asks for.

A percentage over one review is a fact about one person. The listing therefore
treats a Steam score under the threshold as no score. It matches no Steam range
and sorts with the unscored. The work itself stays listed, and its card still
shows the percentage beside its count (ADR-0029).

The default is the store's own verdict threshold. In 584 real answers the store
named no verdict at 1, 2 and 5 reviews and named one from 16 up, which fits the
10 it states. So the default lists what the store would rank, and a user who
wants the small games lowers it.
"""

from typing import Any, Final

from sqlalchemy import ColumnElement, case, literal_column

from ludarium.models import Work

DEFAULT_STEAM_REVIEWS: Final = 10
# Past the most-reviewed game on the store, by two orders of magnitude.
MAX_STEAM_REVIEWS: Final = 1_000_000_000


def steam_score(threshold: int) -> ColumnElement[Any]:
    """The Steam percentage where it rests on at least `threshold` reviews, else null.

    The threshold is written into the SQL rather than bound. SQLite uses an
    expression index only for the same expression, and the one over the default
    (`models.catalogue`) names 10, not a parameter. It is a validated integer,
    so writing it in is safe.
    """

    reviews: ColumnElement[int] = literal_column(str(int(threshold)))
    return case((Work.steam_review_count >= reviews, Work.steam_review_percent))


def steam_score_of(work: Work, threshold: int) -> int | None:
    """`steam_score` read off a loaded row, for the cursor."""

    count, percent = work.steam_review_count, work.steam_review_percent
    return percent if count is not None and count >= threshold else None
