"""A safe method must not write, because `get_session` takes it at its word.

The transaction mode comes from the HTTP method: a GET gets `BEGIN DEFERRED`
and holds a shared lock. A GET that then writes has to upgrade that lock, which
is the one case SQLite refuses outright rather than waiting out — and only when
something else holds a shared lock at the same moment, so the bug would show up
as an occasional `database is locked` and never in a test.

`PRAGMA query_only` is what enforces this, at runtime and completely. What this
adds is the half that does not need the endpoint to have a test: a `GET` nobody
exercises would otherwise reach production before the database refused it.

It reads the handler, not the helpers below it. `sync_account` is the only
writer of that shape and only a `POST` reaches it.
"""

import ast
from pathlib import Path
from typing import Final

API: Final = Path(__file__).resolve().parents[1] / "src" / "ludarium" / "api"
SAFE: Final = frozenset({"get", "head", "options"})

# `commit` and `flush` alone were the first version of this guard, and they are
# not the hazard: the lock upgrade happens at the write statement, so a handler
# that writes and never commits deadlocks just the same. These are the names by
# which a write enters a handler — the DML constructors, the ORM's staging
# calls, and the two that push the result out.
WRITES: Final = frozenset(
    {
        "commit",
        "flush",
        "add",
        "add_all",
        "delete",
        "merge",
        "update",
        "insert",
        "bulk_save_objects",
    }
)

# Raw SQL reaches the database with none of those names anywhere near it.
DML: Final = frozenset({"update", "insert", "delete", "replace", "create", "drop", "alter"})


def route_methods(decorator: ast.expr) -> set[str]:
    """The HTTP methods one decorator declares, however it declares them.

    `@router.get("")` names its method in the attribute; `@router.api_route(path,
    methods=["GET"])` is equally valid FastAPI and names it in a keyword. A guard
    that understood only the first shape would skip the second in silence.
    """

    if not isinstance(decorator, ast.Call):
        return set()
    function = decorator.func
    if not (isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)):
        return set()
    if function.attr != "api_route":
        return {function.attr.lower()}
    return {
        element.value.lower()
        for keyword in decorator.keywords
        if keyword.arg == "methods" and isinstance(keyword.value, ast.List)
        for element in keyword.value.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }


def called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def written_by(node: ast.AST) -> set[str]:
    """Names called anywhere in the handler that mean a write.

    Deliberately blunt about `delete` and `update`: a false positive is a test
    telling someone to use a `POST`, and a false negative is an intermittent
    `database is locked` that nobody can reproduce.
    """

    found: set[str] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        name = called_name(call)
        if name in WRITES:
            found.add(name)
        if name == "text":
            found |= {
                verb
                for argument in call.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                for verb in [argument.value.strip().split(" ")[0].lower()]
                if verb in DML
            }
    return found


def safe_handlers(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and any(route_methods(decorator) & SAFE for decorator in node.decorator_list)
    ]


def test_no_safe_method_writes() -> None:
    offenders = {
        f"{path.name}:{handler.name}": sorted(names)
        for path in sorted(API.rglob("*.py"))
        for handler in safe_handlers(ast.parse(path.read_text()))
        if (names := written_by(handler))
    }

    assert offenders == {}, "a safe method may not write — the session refuses it"


def test_the_guard_finds_the_handlers_it_claims_to_check() -> None:
    """Passing because the walk found nothing would look exactly the same."""

    found = [
        handler.name
        for path in sorted(API.rglob("*.py"))
        for handler in safe_handlers(ast.parse(path.read_text()))
    ]

    assert "listing" in found


def only(source: str) -> set[str]:
    return written_by(ast.parse(source))


def test_a_write_with_no_commit_anywhere_near_it_is_still_a_write() -> None:
    """The version this guard replaces saw nothing wrong with any of these."""

    assert only("await session.execute(update(Work).values(title='x'))") == {"update"}
    assert only("await session.execute(delete(Work))") == {"delete"}
    assert only("await session.execute(text('UPDATE work SET title = 1'))") == {"update"}
    assert only("session.add(Work(title='x'))") == {"add"}


def test_a_read_is_not_a_write() -> None:
    """Or the guard would fail on every endpoint at once and mean nothing."""

    assert only("await session.execute(select(Work).where(Work.id == 1))") == set()
    assert only("await session.execute(text('SELECT 1'))") == set()
    assert only("await session.scalars(select(Work))") == set()


def test_the_guard_would_notice_one() -> None:
    tree = ast.parse(
        "@router.get('')\nasync def bad(session: SessionDep) -> None:\n    await session.commit()\n"
    )
    handlers = safe_handlers(tree)

    assert [handler.name for handler in handlers] == ["bad"]
    assert written_by(handlers[0]) == {"commit"}


def test_a_method_declared_in_a_keyword_is_still_a_method() -> None:
    """`api_route` is ordinary FastAPI, and the first version of this was blind to it."""

    tree = ast.parse(
        "@router.api_route('/x', methods=['GET'])\n"
        "async def sneaky(session: SessionDep) -> None:\n"
        "    await session.commit()\n"
    )

    assert [handler.name for handler in safe_handlers(tree)] == ["sneaky"]


def test_a_post_is_not_a_safe_method() -> None:
    tree = ast.parse(
        "@router.post('')\n"
        "async def fine(session: SessionDep) -> None:\n"
        "    await session.commit()\n"
    )

    assert safe_handlers(tree) == []
