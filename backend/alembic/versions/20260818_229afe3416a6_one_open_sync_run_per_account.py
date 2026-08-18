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
    # The index cannot be created over rows that already violate it, and the
    # database most likely to hold two open runs for one account is exactly the
    # one this revision exists for: a process killed mid-sync, twice. Without
    # this the upgrade fails outright and the instance cannot be started at all,
    # which is a worse outcome than the overlap.
    #
    # Every open run is closed, not merely the surplus: a schema migration runs
    # with the application stopped, so a row still saying `running` is one
    # nothing is left alive to finish. `failed` rather than `success`, so none of
    # them can be credited with a removal (rule 1). Not reversed on downgrade —
    # which rows had been open is not recoverable, and inventing an answer would
    # be worse than leaving them closed.
    op.execute(
        sa.text(
            "UPDATE sync_run SET status = 'failed', finished_at = CURRENT_TIMESTAMP, "
            "error_text = 'abandoned; open when the one-run-per-account index was added' "
            "WHERE status = 'running'"
        )
    )
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
