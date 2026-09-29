"""how many library entries a run could not read

Revision ID: 0e652a2e8cf9
Revises: bf0a91f7dc86
Create Date: 2026-09-29 12:20:10.523391

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0e652a2e8cf9"
down_revision: str | Sequence[str] | None = "bf0a91f7dc86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# SQLite's batch mode rebuilds the table and recreates its indexes from
# reflection, which loses `DESC`. These are recreated as they were made.
DESCENDING: tuple[tuple[str, list[str | sa.TextClause]], ...] = (
    ("ix_sync_run_provider_id_started_at", ["provider_id", sa.text("started_at DESC")]),
    (
        "ix_sync_run_account_id_status_started_at",
        ["account_id", "status", sa.text("started_at DESC")],
    ),
)


def upgrade() -> None:
    # A plain ADD COLUMN, which SQLite does in place: no rebuild, so the
    # indexes are untouched. Earlier runs read zero, which is what they skipped.
    op.add_column(
        "sync_run",
        sa.Column("items_skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    with op.batch_alter_table("sync_run") as batch:
        batch.drop_column("items_skipped")
    for name, columns in DESCENDING:
        op.drop_index(name, table_name="sync_run")
        op.create_index(name, "sync_run", columns)
