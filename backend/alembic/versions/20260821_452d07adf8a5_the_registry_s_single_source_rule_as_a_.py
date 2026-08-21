"""the registry's single_source rule as a constraint

Revision ID: 452d07adf8a5
Revises: 229afe3416a6
Create Date: 2026-08-21 09:41:02.118374

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "452d07adf8a5"
down_revision: str | Sequence[str] | None = "229afe3416a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX = "uq_field_provenance_entity_type_entity_id_field_sole_source"
# Spelled out rather than imported from `STRATEGIES`, for the same reason the
# previous revision spells out its status: a migration describes the database
# at this revision, and a later change to the registry must not reach back and
# change what this backfill meant.
SINGLE_SOURCE = (
    "(entity_type = 'work' AND field IN ('title', 'metacritic_score', 'metacritic_url')) "
    "OR (entity_type = 'entitlement' AND field IN ('provider_title', 'provider_item_id'))"
)


def upgrade() -> None:
    op.add_column(
        "field_provenance",
        sa.Column("sole_source", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # The correlated subquery is not the scan it looks like: the 5-tuple UNIQUE
    # constraint's autoindex starts with (entity_type, entity_id, field), so
    # the rival lookup is a covering search on it. Measured over 80 000 rows —
    # a 20 000-game library — 105 ms, against 84 ms for the anti-join form that
    # says the same thing less plainly.
    #
    # A group that already holds two non-manual rows is the thing this index
    # exists to prevent, and it cannot be created over one. Rather than pick a
    # winner between them — which would silently bless a misclassification —
    # the backfill leaves the whole group unflagged, and `_only` goes on
    # refusing it at every resolve. The index guards it from growing a third.
    op.execute(
        sa.text(
            f"UPDATE field_provenance SET sole_source = TRUE WHERE ({SINGLE_SOURCE}) "
            "AND source_kind <> 'manual' AND NOT EXISTS ("
            "SELECT 1 FROM field_provenance AS rival "
            "WHERE rival.entity_type = field_provenance.entity_type "
            "AND rival.entity_id = field_provenance.entity_id "
            "AND rival.field = field_provenance.field "
            "AND rival.source_kind <> 'manual' AND rival.id <> field_provenance.id)"
        )
    )
    op.create_index(
        INDEX,
        "field_provenance",
        ["entity_type", "entity_id", "field"],
        unique=True,
        sqlite_where=sa.text("sole_source"),
        postgresql_where=sa.text("sole_source"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="field_provenance")
    op.drop_column("field_provenance", "sole_source")
