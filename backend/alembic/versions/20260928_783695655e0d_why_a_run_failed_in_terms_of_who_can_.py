"""why a run failed, in terms of who can fix it

Revision ID: 783695655e0d
Revises: 7830e4f77212
Create Date: 2026-09-28 00:32:54.334206

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "783695655e0d"
down_revision: str | Sequence[str] | None = "7830e4f77212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from `SyncErrorKind`, so a later value cannot
# reach back and change what this revision created.
KINDS = ("credentials", "not_visible", "rate_limited", "unavailable", "malformed", "other")
# The name the naming convention turns into `ck_sync_run_sync_error_kind`.
CHECK = "sync_error_kind"
# Wide enough for the longest kind, `rate_limited`.
KIND = sa.String(length=12)
# SQLite's batch mode rebuilds the table and recreates its indexes from
# reflection, which loses `DESC`. These are recreated as they were made.
DESCENDING: tuple[tuple[str, list[str | sa.TextClause]], ...] = (
    ("ix_sync_run_provider_id_started_at", ["provider_id", sa.text("started_at DESC")]),
    (
        "ix_sync_run_account_id_status_started_at",
        ["account_id", "status", sa.text("started_at DESC")],
    ),
)


def _restore_descending_indexes() -> None:
    for name, columns in DESCENDING:
        op.drop_index(name, table_name="sync_run")
        op.create_index(name, "sync_run", columns)


def upgrade() -> None:
    # Runs that failed before this revision keep a null kind: their message is
    # all there is to go on.
    with op.batch_alter_table("sync_run") as batch:
        batch.add_column(sa.Column("error_kind", KIND, nullable=True))
        batch.create_check_constraint(
            CHECK, f"error_kind IN ({', '.join(f"'{kind}'" for kind in KINDS)})"
        )
    _restore_descending_indexes()


def downgrade() -> None:
    with op.batch_alter_table("sync_run") as batch:
        batch.drop_constraint(op.f(f"ck_sync_run_{CHECK}"), type_="check")
        batch.drop_column("error_kind")
    _restore_descending_indexes()
