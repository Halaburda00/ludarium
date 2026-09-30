"""indexes for each listing order over work

Revision ID: 301947e75a3a
Revises: 88c7d4063f34
Create Date: 2026-09-30 13:31:02.227821

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "301947e75a3a"
down_revision: str | Sequence[str] | None = "88c7d4063f34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The `work` columns the listing can be ordered by. Playtime and last played
# are on `user_work_state`, which the listing outer-joins, and get none.
COLUMNS = ("metacritic_score", "steam_review_percent", "release_date")


def upgrade() -> None:
    for column in COLUMNS:
        nulls = sa.text(f"{column} IS NULL")
        op.create_index(f"ix_work_{column}_asc", "work", [nulls, column, "sort_key", "id"])
        op.create_index(
            f"ix_work_{column}_desc",
            "work",
            [nulls, sa.text(f"{column} DESC"), "sort_key", "id"],
        )


def downgrade() -> None:
    for column in COLUMNS:
        op.drop_index(f"ix_work_{column}_desc", "work")
        op.drop_index(f"ix_work_{column}_asc", "work")
