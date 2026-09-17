"""a fetch cache and one open enrichment run per provider

Revision ID: 870cd4b67e0c
Revises: 8991e6d21a91
Create Date: 2026-09-17 00:34:08.324641

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "870cd4b67e0c"
down_revision: str | Sequence[str] | None = "8991e6d21a91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "uq_sync_run_provider_id_running_unattached"
# Spelled out rather than imported from `SyncStatus`, as the per-account index
# before it is. Nothing before this revision opened a run without an account,
# so there is no existing row the index could be created over.
PREDICATE = "status = 'running' AND account_id IS NULL"


def upgrade() -> None:
    op.create_table(
        "fetch_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON(none_as_null=True).with_variant(
                postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name=op.f("fk_fetch_cache_provider_id_provider"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_cache")),
        sa.UniqueConstraint(
            "provider_id", "resource", "key", name=op.f("uq_fetch_cache_provider_id_resource_key")
        ),
    )
    op.create_index(
        "ix_fetch_cache_provider_id_resource_fetched_at",
        "fetch_cache",
        ["provider_id", "resource", "fetched_at"],
    )
    op.create_index(
        INDEX,
        "sync_run",
        ["provider_id"],
        unique=True,
        sqlite_where=sa.text(PREDICATE),
        postgresql_where=sa.text(PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="sync_run")
    op.drop_index("ix_fetch_cache_provider_id_resource_fetched_at", table_name="fetch_cache")
    op.drop_table("fetch_cache")
