"""entitlements a user keeps

Revision ID: 0803d4373417
Revises: f8be67b5ba2e
Create Date: 2026-10-01 15:14:43.219201

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0803d4373417"
down_revision: str | Sequence[str] | None = "f8be67b5ba2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A plain ADD COLUMN, which SQLite does in place. Nothing was kept before
    # the restore that sets it existed (ADR-0030).
    op.add_column("entitlement", sa.Column("kept_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("entitlement") as batch:
        batch.drop_column("kept_at")
