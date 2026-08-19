"""A safe method must not write, because `get_session` takes it at its word.

The transaction mode comes from the HTTP method: a GET gets `BEGIN DEFERRED`
and holds a shared lock. A GET that then writes has to upgrade that lock, which
is the one case SQLite refuses outright rather than waiting out — and it refuses
only when something else is holding a shared lock at the same moment, so the bug
would appear as an occasional `database is locked` and never in a test.

Read by parsing rather than by running: the failure needs concurrency to show
itself, and this needs only the source. It sees a handler that commits, not a
helper three calls down that does — `sync_account` is the only writer of that
shape and only a POST reaches it.
"""

import ast
from pathlib import Path
from typing import Final

API: Final = Path(__file__).resolve().parents[1] / "src" / "ludarium" / "api"
SAFE: Final = frozenset({"get", "head", "options"})


def route_method(decorator: ast.expr) -> str | None:
    """`@router.get("")` → `get`. Anything else is not a route decorator."""

    if not isinstance(decorator, ast.Call):
        return None
    function = decorator.func
    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        return function.attr
    return None


def commits(node: ast.AST) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in {"commit", "flush"}
        for call in ast.walk(node)
    )


def safe_handlers(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and any(route_method(decorator) in SAFE for decorator in node.decorator_list)
    ]


def test_no_safe_method_writes() -> None:
    offenders = [
        f"{path.name}:{handler.name}"
        for path in sorted(API.rglob("*.py"))
        for handler in safe_handlers(ast.parse(path.read_text()))
        if commits(handler)
    ]

    assert offenders == [], "a GET holds a shared lock it cannot upgrade — use POST"


def test_the_guard_finds_the_handlers_it_claims_to_check() -> None:
    """Passing because the walk found nothing would look exactly the same."""

    found = [
        handler.name
        for path in sorted(API.rglob("*.py"))
        for handler in safe_handlers(ast.parse(path.read_text()))
    ]

    assert "listing" in found


def test_the_guard_would_notice_one() -> None:
    tree = ast.parse(
        "@router.get('')\nasync def bad(session: SessionDep) -> None:\n    await session.commit()\n"
    )
    handlers = safe_handlers(tree)

    assert [handler.name for handler in handlers] == ["bad"]
    assert commits(handlers[0])


def test_a_post_is_not_a_safe_method() -> None:
    tree = ast.parse(
        "@router.post('')\n"
        "async def fine(session: SessionDep) -> None:\n"
        "    await session.commit()\n"
    )

    assert safe_handlers(tree) == []
