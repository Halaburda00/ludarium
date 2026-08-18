"""one open sync run per account

Revision ID: 229afe3416a6
Revises: edc68f600d75
Create Date: 2026-08-18 16:02:43.287379

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "229afe3416a6"
down_revision: str | Sequence[str] | None = "edc68f600d75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX = "uq_sync_run_account_id_running"
# Spelled out rather than imported from `SyncStatus`: a migration describes the
# database as it was at this revision, and a later rename of the enum member
# must not silently change what this index means.
PREDICATE = "status = 'running' AND account_id IS NOT NULL"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "sync_run",
        ["account_id"],
        unique=True,
        sqlite_where=sa.text(PREDICATE),
        postgresql_where=sa.text(PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="sync_run")
