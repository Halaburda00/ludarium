"""genres and the works they describe

Revision ID: f8be67b5ba2e
Revises: b844c9abe67b
Create Date: 2026-09-30 19:07:53.661464

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8be67b5ba2e"
down_revision: str | Sequence[str] | None = "b844c9abe67b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "genre",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_genre")),
        sa.UniqueConstraint("slug", name=op.f("uq_genre_slug")),
    )
    op.create_table(
        "work_genre",
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("genre_id", sa.Integer(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["genre_id"],
            ["genre.id"],
            name=op.f("fk_work_genre_genre_id_genre"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["work.id"],
            name=op.f("fk_work_genre_work_id_work"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("work_id", "genre_id", name=op.f("pk_work_genre")),
    )
    # The details step's cached answers were kept without genres, and would be
    # read for up to a month before IGDB was asked again. Dropped, the next run
    # asks, one request per 500 matched works. A cache holds nothing a run
    # cannot fetch again.
    op.execute("DELETE FROM fetch_cache WHERE resource = 'games/details'")


def downgrade() -> None:
    op.drop_table("work_genre")
    op.drop_table("genre")
