"""a match audit, the trail a merge is undone from

Revision ID: 31c02c12812f
Revises: 1a048697cd31
Create Date: 2026-09-27 15:27:08.057278

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "31c02c12812f"
down_revision: str | Sequence[str] | None = "1a048697cd31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from the enums, so a later value cannot reach
# back and change what this revision created.
MATCH_ACTIONS = ("linked", "unlinked", "relinked", "merged", "unmerged")
MATCH_LAYERS = ("hard_id", "alias", "fuzzy", "llm", "manual")
MATCH_ACTORS = ("auto", "user")


def upgrade() -> None:
    op.create_table(
        "match_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entitlement_id", sa.Integer(), nullable=True),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(*MATCH_ACTIONS, name="match_action", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column(
            "layer",
            sa.Enum(*MATCH_LAYERS, name="match_layer", native_enum=False, create_constraint=True),
            nullable=True,
        ),
        sa.Column("previous_work_id", sa.Integer(), nullable=True),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "actor",
            sa.Enum(*MATCH_ACTORS, name="match_actor", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_audit")),
    )
    op.create_index("ix_match_audit_work_id", "match_audit", ["work_id"])


def downgrade() -> None:
    op.drop_index("ix_match_audit_work_id", table_name="match_audit")
    op.drop_table("match_audit")
