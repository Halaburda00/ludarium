"""twitch app tokens kept across restarts

Revision ID: 8991e6d21a91
Revises: cb303273d67a
Create Date: 2026-09-11 03:36:00.369707

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8991e6d21a91"
down_revision: str | Sequence[str] | None = "cb303273d67a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "twitch_app_token",
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_twitch_app_token")),
    )


def downgrade() -> None:
    op.drop_table("twitch_app_token")
