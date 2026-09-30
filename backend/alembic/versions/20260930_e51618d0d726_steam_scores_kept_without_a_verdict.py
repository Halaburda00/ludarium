"""steam scores kept without a verdict

Revision ID: e51618d0d726
Revises: b64d53f59fa9
Create Date: 2026-09-30 15:25:42.203548

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e51618d0d726"
down_revision: str | Sequence[str] | None = "b64d53f59fa9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The Steam order compares this rather than the percentage, now that a
# percentage is kept without a verdict (#107). Spelled as `sorting` writes it:
# SQLite uses an expression index only for the same expression.
SCORE = "CASE WHEN steam_review_rating IS NOT NULL THEN steam_review_percent END"


def _create(name: str, value: str) -> None:
    op.create_index(
        f"ix_work_{name}_asc",
        "work",
        [sa.text(f"({value}) IS NULL"), sa.text(value), "sort_key", "id"],
    )
    op.create_index(
        f"ix_work_{name}_desc",
        "work",
        [sa.text(f"({value}) IS NULL"), sa.text(f"({value}) DESC"), "sort_key", "id"],
    )


def _drop(name: str) -> None:
    op.drop_index(f"ix_work_{name}_desc", "work")
    op.drop_index(f"ix_work_{name}_asc", "work")


def upgrade() -> None:
    _drop("steam_review_percent")
    _create("steam_review_score", SCORE)


def downgrade() -> None:
    _drop("steam_review_score")
    _create("steam_review_percent", "steam_review_percent")
