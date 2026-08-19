"""One rule, checked by reading the source, because reading the code missed it three times.

`user_id` and `removed_at` are the predicates whose omission is silent: without
the first one account's library leaks into another's, without the second rows
that rule 1 marked as removed come back. Mutation testing found the pair missing
from three separate endpoints — the sync endpoints in #29 and both queries in
#11 — every time because a fixture with one user cannot tell a scoped query from
an unscoped one.

Individual tests with a second user are still the ones that catch a real leak.
This is what stops the next endpoint from needing to remember.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "ludarium"
# Where the predicate is allowed to be written out: the helper that defines it,
# and the sync service, which filters entitlements by account rather than by
# user and has its own tests for that.
ALLOWED = {"queries.py", "sync.py"}


def modules() -> list[Path]:
    return [path for path in SOURCE.rglob("*.py") if path.name not in ALLOWED]


def attribute_pairs(tree: ast.AST) -> set[str]:
    """`Entitlement.user_id` and friends, as they appear in a query."""

    return {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }


def test_no_query_writes_the_ownership_predicate_by_hand() -> None:
    offenders = {
        str(path.relative_to(SOURCE)): sorted(names)
        for path in modules()
        if (
            names := attribute_pairs(ast.parse(path.read_text()))
            & {
                "Entitlement.user_id",
                "Entitlement.removed_at",
            }
        )
    }

    assert offenders == {}, "use `ludarium.queries.owned_by` instead"


def test_the_guard_would_notice_one() -> None:
    """It passes on an empty set as readily as on a correct tree."""

    written_out = ast.parse("select(Entitlement).where(Entitlement.user_id == 1)")

    assert "Entitlement.user_id" in attribute_pairs(written_out)
