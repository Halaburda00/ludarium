"""the steam store's review summary on a work

Revision ID: 1341c82a2432
Revises: 783695655e0d
Create Date: 2026-09-28 16:56:53.368874

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1341c82a2432"
down_revision: str | Sequence[str] | None = "783695655e0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from `SteamRating`, so a later value cannot reach
# back and change what this revision created.
RATINGS = (
    "overwhelmingly_negative",
    "very_negative",
    "negative",
    "mostly_negative",
    "mixed",
    "mostly_positive",
    "positive",
    "very_positive",
    "overwhelmingly_positive",
)
# The name the naming convention turns into `ck_work_steam_rating`.
CHECK = "steam_rating"
# Wide enough for the longest rating, `overwhelmingly_positive`.
RATING = sa.String(length=23)


def upgrade() -> None:
    # Null everywhere until the store's step has run: nothing has asked yet.
    with op.batch_alter_table("work") as batch:
        batch.add_column(sa.Column("steam_review_rating", RATING, nullable=True))
        batch.add_column(sa.Column("steam_review_percent", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("steam_review_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("steam_review_appid", sa.Text(), nullable=True))
        batch.create_check_constraint(
            CHECK, f"steam_review_rating IN ({', '.join(f"'{rating}'" for rating in RATINGS)})"
        )


def downgrade() -> None:
    with op.batch_alter_table("work") as batch:
        batch.drop_constraint(op.f(f"ck_work_{CHECK}"), type_="check")
        batch.drop_column("steam_review_appid")
        batch.drop_column("steam_review_count")
        batch.drop_column("steam_review_percent")
        batch.drop_column("steam_review_rating")
