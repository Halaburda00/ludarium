"""add-ons found under their game

Revision ID: f878bd6caf3a
Revises: e10c9a98faed
Create Date: 2026-10-03 00:06:31.444475

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f878bd6caf3a"
down_revision: str | Sequence[str] | None = "e10c9a98faed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Partial: nearly every work is a game with no parent, and only the add-ons
    # need finding under one (#98).
    op.create_index(
        "ix_work_parent_work_id",
        "work",
        ["parent_work_id"],
        sqlite_where=sa.text("parent_work_id IS NOT NULL"),
        postgresql_where=sa.text("parent_work_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_work_parent_work_id", table_name="work")
