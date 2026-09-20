"""a playtest is an item kind and a work may be unclassified

Revision ID: 9bae6dfde8a2
Revises: 870cd4b67e0c
Create Date: 2026-09-17 02:54:09.146568

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9bae6dfde8a2"
down_revision: str | Sequence[str] | None = "870cd4b67e0c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from `ItemKind`, so a later value cannot reach
# back and change what this revision created.
BEFORE = ("game", "dlc", "demo", "soundtrack", "video", "tool", "mod")
AFTER = ("game", "dlc", "demo", "playtest", "soundtrack", "video", "tool", "mod")
# The name the naming convention gives the enum's CHECK, on both tables.
CHECK = "item_kind"
# `soundtrack` is still the longest value, so the column keeps its width.
KIND = sa.String(length=10)


def _within(values: tuple[str, ...]) -> str:
    return f"item_kind IN ({', '.join(f"'{value}'" for value in values)})"


def upgrade() -> None:
    # Both tables are rebuilt, and `work` is referenced with ON DELETE CASCADE.
    # Safe for the reason `cb303273d67a` gives: Alembic's connection never turns
    # `foreign_keys` on.
    with op.batch_alter_table("entitlement") as batch:
        batch.drop_constraint(op.f(f"ck_entitlement_{CHECK}"), type_="check")
        batch.create_check_constraint(CHECK, _within(AFTER))
    with op.batch_alter_table("work") as batch:
        batch.drop_constraint(op.f(f"ck_work_{CHECK}"), type_="check")
        batch.alter_column("item_kind", existing_type=KIND, nullable=True, server_default=None)
        batch.create_check_constraint(CHECK, _within(AFTER))

    # `game` on a work no source asserts a kind for is the old default, not an
    # answer, and it is exactly what the new null exists to stop claiming.
    # A work the resolver did classify keeps its value.
    op.execute(
        """
        UPDATE work SET item_kind = NULL
        WHERE item_kind = 'game'
          AND NOT EXISTS (
            SELECT 1 FROM field_provenance
            WHERE entity_type = 'work' AND entity_id = work.id AND field = 'item_kind'
          )
        """
    )


def downgrade() -> None:
    # Lossy by necessity: the older schema has no word for either. An
    # unclassified work goes back to the default it used to carry, and a
    # playtest becomes unknown where unknown can be said. The provenance rows
    # asserting `playtest` go, because the older resolver cannot cast them and
    # would fail on the first resolve that read one; enrichment asks again.
    op.execute(
        "UPDATE work SET item_kind = 'game' WHERE item_kind IS NULL OR item_kind = 'playtest'"
    )
    op.execute("UPDATE entitlement SET item_kind = NULL WHERE item_kind = 'playtest'")
    op.execute("DELETE FROM field_provenance WHERE field = 'item_kind' AND value = '\"playtest\"'")
    with op.batch_alter_table("work") as batch:
        batch.drop_constraint(op.f(f"ck_work_{CHECK}"), type_="check")
        batch.alter_column(
            "item_kind", existing_type=KIND, nullable=False, server_default=sa.text("'game'")
        )
        batch.create_check_constraint(CHECK, _within(BEFORE))
    with op.batch_alter_table("entitlement") as batch:
        batch.drop_constraint(op.f(f"ck_entitlement_{CHECK}"), type_="check")
        batch.create_check_constraint(CHECK, _within(BEFORE))
