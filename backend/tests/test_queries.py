"""`in_batches`, which stands between a large library and a run that cannot finish.

The failure it prevents is not a slow query. Past the driver's ceiling on bound
parameters the statement raises, and it raises inside the run's own transaction
— so the sync rolls back, reports `failed`, and does so identically on every
retry until the library shrinks. The account is stuck rather than slow.
"""

import pytest

from ludarium import queries
from ludarium.queries import BIND_LIMIT, in_batches


def test_a_list_that_fits_is_one_batch() -> None:
    assert [list(batch) for batch in in_batches([1, 2, 3])] == [[1, 2, 3]]


def test_nothing_is_no_batches_rather_than_one_empty_one() -> None:
    """`IN ()` matches nothing on some engines and is a syntax error on others.

    Yielding nothing makes the caller's `for` loop do nothing, which is the
    honest reading of "there is no id to ask about".
    """

    assert list(in_batches([])) == []


def test_a_list_that_does_not_fit_is_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queries, "BIND_LIMIT", 2)

    assert [list(batch) for batch in in_batches([1, 2, 3, 4, 5])] == [[1, 2], [3, 4], [5]]


def test_the_limit_is_under_every_engine_we_target() -> None:
    """999 is SQLite before 3.32, and the number this has to stay under.

    Modern SQLite allows 32766 and PostgreSQL 65535, but the container is not
    the only place this runs — a NAS with an older system SQLite is exactly the
    deployment the project is for.
    """

    assert BIND_LIMIT < 999
