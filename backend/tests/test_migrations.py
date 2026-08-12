from pathlib import Path

from alembic import command
from conftest import alembic_config, create_schema, sync_url
from sqlalchemy import create_engine, inspect, text

from ludarium.config import Settings


def table_names(url: str) -> set[str]:
    engine = create_engine(sync_url(url))
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def split_top_level(body: str) -> list[str]:
    """Split on commas that separate parts, not on commas inside them.

    Quoting matters as much as nesting here: a `CHECK (x IN ('a, b'))` or a
    server default with a comma in it would otherwise be torn in half.
    """

    items: list[str] = []
    depth = 0
    quote = ""
    current = ""
    for character in body:
        if quote:
            quote = "" if character == quote else quote
        elif character in "'\"":
            quote = character
        elif character == "," and depth == 0:
            items.append(current.strip())
            current = ""
            continue
        else:
            depth += (character == "(") - (character == ")")
        current += character
    items.append(current.strip())
    return items


def normalise(sql: str) -> str:
    """Whitespace and the order of the parts inside `CREATE TABLE (...)`.

    SQLAlchemy emits constraints in the order the constraint objects were
    constructed, which differs between the two paths by construction:
    `__table_args__` is evaluated with the class body, an inline `ForeignKey`
    becomes a constraint when the table is assembled, and an `Enum` `CHECK`
    later still. Ordering says nothing about whether the two schemas agree, so
    the comparison does not look at it.
    """

    flattened = " ".join(sql.split())
    opening = flattened.find("(")
    if not flattened.startswith("CREATE TABLE") or opening == -1:
        return flattened
    head, body = flattened[:opening], flattened[opening + 1 : flattened.rindex(")")]
    return f"{head}({', '.join(sorted(split_top_level(body)))})"


def schema_dump(url: str) -> dict[str, str]:
    """Every CREATE statement SQLite kept, normalised."""

    engine = create_engine(sync_url(url))
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND name <> 'alembic_version'"
                )
            )
            return {name: normalise(sql) for name, sql in rows}
    finally:
        engine.dispose()


# Sync tests on purpose: env.py drives the async engine with asyncio.run, which
# cannot be called from inside a running loop.
def test_upgrade_then_downgrade_leaves_an_empty_database(settings: Settings) -> None:
    config = alembic_config(settings.database_url)

    command.upgrade(config, "head")
    after_upgrade = table_names(settings.database_url)
    command.downgrade(config, "base")

    assert after_upgrade == {
        "alembic_version",
        "app_user",
        "session",
        "provider",
        "account",
        "sync_run",
        "work",
        "edition",
        "entitlement",
        "entitlement_work",
        "user_work_state",
        "field_provenance",
    }
    assert table_names(settings.database_url) == {"alembic_version"}


def test_upgrade_creates_the_database_directory(tmp_path: Path) -> None:
    """A fresh checkout has no `data/`, and this is the first command we document."""

    url = f"sqlite+aiosqlite:///{tmp_path / 'data' / 'ludarium.db'}"

    command.upgrade(alembic_config(url), "head")

    assert "provider" in table_names(url)


def test_the_migration_and_the_models_agree(settings: Settings) -> None:
    """Hand-written migrations drift. This is what notices.

    Alembic's own `compare_metadata` is not usable here: it cannot tell that the
    enum `CHECK`s belong to the type, and SQLite cannot reflect the expression
    indexes on `sync_run` at all. Comparing the DDL both paths produce can.
    """

    command.upgrade(alembic_config(settings.database_url), "head")
    from_models = settings.database_url.replace("ludarium.db", "from_models.db")
    create_schema(from_models)

    assert schema_dump(settings.database_url) == schema_dump(from_models)
