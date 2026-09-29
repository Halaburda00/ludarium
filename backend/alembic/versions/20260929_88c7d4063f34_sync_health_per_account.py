"""sync health per account

Revision ID: 88c7d4063f34
Revises: 0e652a2e8cf9
Create Date: 2026-09-29 13:26:26.299892

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "88c7d4063f34"
down_revision: str | Sequence[str] | None = "0e652a2e8cf9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Spelled out rather than read from `SyncStatus`, so a later value cannot reach
# back and change what this revision created.
STATUSES = ("pending", "running", "success", "partial", "failed")
# The name the naming convention turns into `ck_account_sync_status`.
CHECK = "sync_status"
# Wide enough for the longest status.
STATUS = sa.String(length=7)


def upgrade() -> None:
    with op.batch_alter_table("account") as batch:
        batch.add_column(
            sa.Column("status", STATUS, server_default=sa.text("'pending'"), nullable=False)
        )
        batch.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch.create_check_constraint(
            CHECK, f"status IN ({', '.join(f"'{status}'" for status in STATUSES)})"
        )
    # Every account that has finished a run says what its latest one did, rather
    # than reading `pending` until its next sync. `running` is left out: an open
    # run has not said anything yet.
    latest = (
        "SELECT {column} FROM sync_run WHERE sync_run.account_id = account.id "
        "AND sync_run.status != 'running' ORDER BY sync_run.id DESC LIMIT 1"
    )
    op.execute(
        f"UPDATE account SET status = ({latest.format(column='status')}), "
        f"last_error = ({latest.format(column='error_text')}) "
        f"WHERE EXISTS ({latest.format(column='1')})"
    )


def downgrade() -> None:
    with op.batch_alter_table("account") as batch:
        batch.drop_constraint(op.f(f"ck_account_{CHECK}"), type_="check")
        batch.drop_column("last_error")
        batch.drop_column("status")
