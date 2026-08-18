import re
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
    """Whitespace, the quoting of the table name, and the order of the parts inside `(...)`.

    SQLAlchemy emits constraints in the order the constraint objects were
    constructed, which differs between the two paths by construction:
    `__table_args__` is evaluated with the class body, an inline `ForeignKey`
    becomes a constraint when the table is assembled, and an `Enum` `CHECK`
    later still. Ordering says nothing about whether the two schemas agree, so
    the comparison does not look at it.

    Nor do two artefacts of `batch_alter_table`, which SQLite forces on any
    change to a constraint: the rebuilt table comes back as `CREATE TABLE
    "entitlement"` where `create_all` writes it bare, and its defaults come back
    parenthesised — `DEFAULT (CURRENT_TIMESTAMP)` for `DEFAULT
    CURRENT_TIMESTAMP`. Both are the same schema spelled differently.

    The unquoting is applied to the table name only, never the body: a CHECK
    compares against string literals, and those are the schema.
    """

    flattened = " ".join(sql.split())
    opening = flattened.find("(")
    if not flattened.startswith("CREATE TABLE") or opening == -1:
        return flattened
    head, body = flattened[:opening], flattened[opening + 1 : flattened.rindex(")")]
    parts = (re.sub(r"DEFAULT \(([^()]*)\)", r"DEFAULT \1", part) for part in split_top_level(body))
    return f"{head.replace('"', '')}({', '.join(sorted(parts))})"


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


def seed_two_open_runs(url: str) -> None:
    """The state the one-run-per-account index cannot be created over.

    Written as raw SQL rather than through the models: this is a database at an
    older revision, and the ORM describes the newest one.
    """

    engine = create_engine(sync_url(url))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO app_user (id, username, password_hash, locale, created_at) "
                    "VALUES (1, 'owner', 'not-a-hash', 'en', CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO provider (id, key, kind, source_kind, licence_class, "
                    "display_name, precedence_weight, enabled, status) VALUES "
                    "(1, 'steam', 'platform', 'platform_api', 'redistributable', "
                    "'Steam', 100, 1, 'pending')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO account (id, user_id, provider_id, label, is_derived, "
                    "is_active, created_at) VALUES (1, 1, 1, 'Main', 0, 1, CURRENT_TIMESTAMP)"
                )
            )
            for run_id in (1, 2):
                connection.execute(
                    text(
                        "INSERT INTO sync_run (id, provider_id, account_id, trigger, status, "
                        "started_at, items_seen, items_added, items_updated, items_removed) "
                        f"VALUES ({run_id}, 1, 1, 'manual', 'running', CURRENT_TIMESTAMP, "
                        "0, 0, 0, 0)"
                    )
                )
    finally:
        engine.dispose()


def test_the_upgrade_survives_a_database_that_already_breaks_the_new_index(
    settings: Settings,
) -> None:
    """An empty database is the easy case, and not the one this index exists for.

    Two open runs for one account is precisely what a process killed mid-sync
    leaves behind, twice. `CREATE UNIQUE INDEX` over that fails, and a failed
    upgrade means the instance cannot start at all — worse than the overlap the
    index is there to prevent.
    """

    config = alembic_config(settings.database_url)
    command.upgrade(config, "edc68f600d75")
    seed_two_open_runs(settings.database_url)

    command.upgrade(config, "head")

    engine = create_engine(sync_url(settings.database_url))
    try:
        with engine.connect() as connection:
            rows = list(connection.execute(text("SELECT status, finished_at FROM sync_run")))
    finally:
        engine.dispose()

    # `failed`, never `success`: neither may be credited with a removal (rule 1).
    assert [status for status, _ in rows] == ["failed", "failed"]
    assert all(finished_at is not None for _, finished_at in rows)
