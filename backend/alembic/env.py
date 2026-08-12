import asyncio
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from ludarium.config import get_settings
from ludarium.db import ensure_sqlite_directory
from ludarium.models import Base
from ludarium.models.types import UtcDateTime

config = context.config

# Tests drive Alembic in-process and set this to False: fileConfig replaces the
# root handlers, which would take pytest's log capture with it.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    # The .ini carries no URL; one instance, one database, and it is configured
    # in the environment like everything else.
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    """Keep application types out of migration scripts.

    A migration is a historical record: it has to keep running after
    `UtcDateTime` is renamed, moved or deleted. The DDL is identical either way.
    """

    if type_ == "type" and isinstance(obj, UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER much: batch mode recreates the table instead.
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = get_url()
    ensure_sqlite_directory(url)
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
