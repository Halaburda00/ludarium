"""saved views

Revision ID: b64d53f59fa9
Revises: 301947e75a3a
Create Date: 2026-09-30 14:09:59.558800

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b64d53f59fa9"
down_revision: str | Sequence[str] | None = "301947e75a3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_view",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_saved_view_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_view")),
        sa.UniqueConstraint("user_id", "name", name=op.f("uq_saved_view_user_id_name")),
    )


def downgrade() -> None:
    op.drop_table("saved_view")
