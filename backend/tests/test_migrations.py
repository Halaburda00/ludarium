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


def schema_dump(url: str) -> dict[str, str]:
    """Every CREATE statement SQLite kept, whitespace-normalised."""

    engine = create_engine(sync_url(url))
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND name <> 'alembic_version'"
                )
            )
            return {name: " ".join(sql.split()) for name, sql in rows}
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
    }
    assert table_names(settings.database_url) == {"alembic_version"}


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
