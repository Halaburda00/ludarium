import re
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
from conftest import alembic_config, create_schema, sync_url
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql

from ludarium.config import Settings
from ludarium.models import Work
from ludarium.titles import sort_key


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


def seed_provenance_before_the_flag(url: str) -> None:
    """A work whose title one source asserts, and a work where two already do.

    Raw SQL for the same reason as above: this is the database one revision
    back, where `sole_source` does not exist yet.
    """

    rows = (
        ("work", 1, "title", "metadata_provider", "igdb"),
        ("work", 1, "title", "manual", "manual"),
        ("work", 2, "title", "metadata_provider", "igdb"),
        ("work", 2, "title", "platform_api", "steam"),
        ("work", 3, "title", "manual", "manual"),
        ("work", 1, "item_kind", "metadata_provider", "igdb"),
        ("entitlement", 5, "provider_title", "platform_api", "steam"),
    )
    engine = create_engine(sync_url(url))
    try:
        with engine.begin() as connection:
            for entity_type, entity_id, field, source_kind, source_ref in rows:
                connection.execute(
                    text(
                        "INSERT INTO field_provenance (entity_type, entity_id, field, "
                        "source_kind, source_ref, value, is_effective, observed_at) VALUES "
                        f"('{entity_type}', {entity_id}, '{field}', '{source_kind}', "
                        f"'{source_ref}', '\"x\"', 0, CURRENT_TIMESTAMP)"
                    )
                )
    finally:
        engine.dispose()


def test_the_backfill_flags_what_the_registry_would_have(settings: Settings) -> None:
    """Including the group it must leave alone, which is the one it could get wrong.

    Work 2 is already asserted by two sources — the state the index exists to
    prevent, and one it cannot be created over. Flagging one of the two would
    have the migration silently pick a winner between them; leaving the group
    unflagged keeps `_only` refusing it at every resolve and still stops a third
    source joining. Neither manual row is flagged: rule 3 puts a user override
    above the strategy, so it has to be able to sit beside a source — and work
    3, where the user got there first and no provider has spoken yet, is the
    one that would otherwise take the slot and refuse the provider later.
    """

    config = alembic_config(settings.database_url)
    command.upgrade(config, "229afe3416a6")
    seed_provenance_before_the_flag(settings.database_url)

    command.upgrade(config, "head")

    engine = create_engine(sync_url(settings.database_url))
    try:
        with engine.connect() as connection:
            flags = {
                row[:-1]: row[-1]
                for row in connection.execute(
                    text(
                        "SELECT entity_type, entity_id, field, source_ref, sole_source "
                        "FROM field_provenance"
                    )
                )
            }
    finally:
        engine.dispose()

    assert flags == {
        ("work", 1, "title", "igdb"): 1,
        ("work", 1, "title", "manual"): 0,
        ("work", 2, "title", "igdb"): 0,
        ("work", 2, "title", "steam"): 0,
        ("work", 3, "title", "manual"): 0,
        ("work", 1, "item_kind", "igdb"): 0,
        ("entitlement", 5, "provider_title", "steam"): 1,
    }


TITLES_BEFORE_THE_KEY = (
    "ARC Raiders",
    "Batman™: Arkham Knight",
    "Brütal Legend",
    "Yu-Gi-Oh!  Master Duel",
    "Witcher 3, The: Wild Hunt",
)
REFERENCING_WORK = ("edition", "user_work_state", "entitlement_work")
SEED_BEFORE_THE_KEY = (
    "INSERT INTO app_user (id, username, password_hash) VALUES (1, 'owner', 'x')",
    "INSERT INTO provider (id, key, kind, source_kind, display_name) "
    "VALUES (1, 'steam', 'platform', 'platform_api', 'Steam')",
    "INSERT INTO account (id, provider_id, label) VALUES (1, 1, 'Main')",
)
SEED_PER_WORK = (
    "INSERT INTO work (id, title, sort_title) VALUES (:id, :title, :title)",
    "INSERT INTO edition (work_id, name, slug) VALUES (:id, 'Standard', 'standard')",
    "INSERT INTO user_work_state (work_id) VALUES (:id)",
    "INSERT INTO entitlement (id, account_id, provider_item_id, provider_title) "
    "VALUES (:id, 1, :id, :title)",
    "INSERT INTO entitlement_work (entitlement_id, work_id) VALUES (:id, :id)",
)


def seed_works_before_the_key(url: str) -> None:
    """Works from before `sort_key` existed, and a row in every table that points at one.

    Raw SQL for the reason above. The references matter as much as the titles:
    adding the column rebuilds `work`, and all three tables cascade on its delete.
    """

    engine = create_engine(sync_url(url))
    try:
        with engine.begin() as connection:
            for statement in SEED_BEFORE_THE_KEY:
                connection.execute(text(statement))
            for work_id, title in enumerate(TITLES_BEFORE_THE_KEY, start=1):
                for statement in SEED_PER_WORK:
                    connection.execute(text(statement), {"id": work_id, "title": title})
    finally:
        engine.dispose()


def test_the_backfill_folds_every_title_and_loses_nothing_that_points_at_one(
    settings: Settings,
) -> None:
    """The migration carries its own copy of `sort_key`, and this is what holds it to the original.

    A copy is the convention — a migration must not change meaning when the
    function it was taken from does — so today the two agree only because a test
    says they do. And the rebuild must not cost a row: `edition`,
    `user_work_state` and `entitlement_work` all cascade on delete, so recreating
    `work` with foreign keys on would empty them without raising anything.
    """

    config = alembic_config(settings.database_url)
    command.upgrade(config, "452d07adf8a5")
    seed_works_before_the_key(settings.database_url)

    command.upgrade(config, "head")

    engine = create_engine(sync_url(settings.database_url))
    try:
        with engine.connect() as connection:
            keys = dict(connection.execute(text("SELECT sort_title, sort_key FROM work")).all())
            counts = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in REFERENCING_WORK
            }
    finally:
        engine.dispose()

    assert keys == {title: sort_key(title) for title in TITLES_BEFORE_THE_KEY}
    assert counts == dict.fromkeys(REFERENCING_WORK, len(TITLES_BEFORE_THE_KEY))


def test_the_revision_gives_postgresql_the_collation_the_model_declares(settings: Settings) -> None:
    """The half of this schema no SQLite test reaches, and the half a real install gets.

    `test_the_migration_and_the_models_agree` compares SQLite DDL, where the
    variant does not exist. On PostgreSQL an instance's column comes from this
    revision rather than from `create_all`, so without this a revision that lost
    `COLLATE "C"` would pass every test and order by locale in production.
    """

    script = ScriptDirectory.from_config(alembic_config(settings.database_url))
    revision = script.get_revision("cb303273d67a")
    assert revision is not None
    dialect = postgresql.dialect()

    migrated = revision.module.SORT_KEY.compile(dialect=dialect)
    modelled = Work.__table__.c.sort_key.type.compile(dialect=dialect)

    assert migrated == modelled == 'TEXT COLLATE "C"'
