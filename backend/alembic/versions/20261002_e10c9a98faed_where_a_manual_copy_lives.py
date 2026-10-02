"""where a manual copy lives

Revision ID: e10c9a98faed
Revises: 0803d4373417
Create Date: 2026-10-02 22:30:48.302020

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e10c9a98faed"
down_revision: str | Sequence[str] | None = "0803d4373417"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain ADD COLUMN, which SQLite does in place. No row has one yet:
    # nothing created a manual entry before the endpoint that sets it (#97).
    op.add_column("entitlement", sa.Column("store_label", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("entitlement") as batch:
        batch.drop_column("store_label")
