"""a manual entitlement carries no provider_item_id

Revision ID: edc68f600d75
Revises: 0b244111705a
Create Date: 2026-08-17 20:11:38.421905

"""

from collections.abc import Sequence

from alembic import op

revision: str = "edc68f600d75"
down_revision: str | Sequence[str] | None = "0b244111705a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "manual_has_no_provider_item_id"


def upgrade() -> None:
    # ADR-0010's claim, made structural. Batch mode because SQLite cannot add a
    # CHECK in place; it recreates the table, which is why the constraint is
    # spelled out here rather than reflected.
    with op.batch_alter_table("entitlement") as batch:
        batch.create_check_constraint(CONSTRAINT, "origin != 'manual' OR provider_item_id IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("entitlement") as batch:
        batch.drop_constraint(CONSTRAINT, type_="check")
