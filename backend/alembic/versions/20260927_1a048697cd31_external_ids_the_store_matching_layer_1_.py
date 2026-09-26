"""external ids, the store matching layer 1 anchors works in

Revision ID: 1a048697cd31
Revises: 9bae6dfde8a2
Create Date: 2026-09-27 00:26:51.332551

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1a048697cd31"
down_revision: str | Sequence[str] | None = "9bae6dfde8a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from `EntityType`, so a later value cannot reach
# back and change what this revision created.
ENTITY_TYPES = ("work", "edition", "entitlement", "account")


def upgrade() -> None:
    op.create_table(
        "external_id",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "entity_type",
            sa.Enum(*ENTITY_TYPES, name="entity_type", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("is_authoritative", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_id")),
        sa.UniqueConstraint(
            "namespace",
            "value",
            "entity_type",
            name=op.f("uq_external_id_namespace_value_entity_type"),
        ),
    )
    op.create_index(
        "ix_external_id_entity_type_entity_id", "external_id", ["entity_type", "entity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_external_id_entity_type_entity_id", table_name="external_id")
    op.drop_table("external_id")
