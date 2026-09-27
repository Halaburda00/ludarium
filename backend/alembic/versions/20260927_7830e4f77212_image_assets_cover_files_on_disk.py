"""image assets, cover files on disk

Revision ID: 7830e4f77212
Revises: 31c02c12812f
Create Date: 2026-09-27 22:13:45.919922

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7830e4f77212"
down_revision: str | Sequence[str] | None = "31c02c12812f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from the enums, so a later value cannot reach
# back and change what this revision created.
ENTITY_TYPES = ("work", "edition", "entitlement", "account")
IMAGE_KINDS = ("cover", "hero", "logo", "screenshot")


def upgrade() -> None:
    op.create_table(
        "image_asset",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "entity_type",
            sa.Enum(*ENTITY_TYPES, name="entity_type", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*IMAGE_KINDS, name="image_kind", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("remote_url", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_image_asset")),
        # See `ImageAsset`: a replaced cover must never reuse a served id.
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_image_asset_entity_type_entity_id", "image_asset", ["entity_type", "entity_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_image_asset_entity_type_entity_id", table_name="image_asset")
    op.drop_table("image_asset")
