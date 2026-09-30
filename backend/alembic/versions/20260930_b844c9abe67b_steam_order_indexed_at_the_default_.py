"""steam order indexed at the default review threshold

Revision ID: b844c9abe67b
Revises: e51618d0d726
Create Date: 2026-09-30 17:46:29.313566

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b844c9abe67b"
down_revision: str | Sequence[str] | None = "e51618d0d726"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The Steam order compares the percentage over at least ten reviews rather than
# over a verdict (#108). Spelled as `scores.steam_score` writes it at the default
# threshold, which is the only one an index can serve.
NAME = "steam_review_score"
OVER_REVIEWS = "CASE WHEN steam_review_count >= 10 THEN steam_review_percent END"
OVER_VERDICT = "CASE WHEN steam_review_rating IS NOT NULL THEN steam_review_percent END"


def _replace(value: str) -> None:
    op.drop_index(f"ix_work_{NAME}_desc", "work")
    op.drop_index(f"ix_work_{NAME}_asc", "work")
    op.create_index(
        f"ix_work_{NAME}_asc",
        "work",
        [sa.text(f"({value}) IS NULL"), sa.text(value), "sort_key", "id"],
    )
    op.create_index(
        f"ix_work_{NAME}_desc",
        "work",
        [sa.text(f"({value}) IS NULL"), sa.text(f"({value}) DESC"), "sort_key", "id"],
    )


def upgrade() -> None:
    _replace(OVER_REVIEWS)


def downgrade() -> None:
    _replace(OVER_VERDICT)
