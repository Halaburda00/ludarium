"""works are ordered by a folded key

Revision ID: cb303273d67a
Revises: 452d07adf8a5
Create Date: 2026-09-11 09:12:44.305118

"""

import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cb303273d67a"
down_revision: str | Sequence[str] | None = "452d07adf8a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `ludarium.titles.sort_key` as it stands at this revision — copied, not
# imported, for the reason the previous revision spells out its registry: a
# migration describes the database at its revision, and a later change to the
# function must not reach back and change what this backfill wrote. A test holds
# the copy equal to the function today; a change to the fold is a new revision
# that recomputes the column (ADR-0018).
MARKS = str.maketrans("", "", "™®©℠")


def sort_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.translate(MARKS))
    folded = unicodedata.normalize("NFKD", decomposed.casefold())
    bare = "".join(character for character in folded if not unicodedata.combining(character))
    return " ".join(bare.split())


def upgrade() -> None:
    # Nullable until it is filled: SQLite adds a NOT NULL column only with a
    # default, and any default would be a key nobody computed.
    op.add_column("work", sa.Column("sort_key", sa.Text(), nullable=True))

    # In Python, because the fold is the one SQL cannot do here: `lower()` on
    # SQLite changes ASCII letters only (ADR-0018).
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, sort_title FROM work")).all()
    if rows:
        connection.execute(
            sa.text("UPDATE work SET sort_key = :key WHERE id = :id"),
            [{"id": work_id, "key": sort_key(title)} for work_id, title in rows],
        )

    # Dropped before the rebuild rather than after, so batch mode does not copy
    # an index this revision is about to remove.
    op.drop_index("ix_work_sort_title_id", table_name="work")
    # Batch mode recreates `work`, and `edition`, `user_work_state` and
    # `entitlement_work` all reference it with ON DELETE CASCADE. That is safe
    # because Alembic's connection is not `Database`'s and never turns
    # `foreign_keys` on. A test seeds a row in each before upgrading and counts
    # them after, so a later change that turns it on fails there rather than on
    # somebody's library.
    with op.batch_alter_table("work") as batch:
        batch.alter_column("sort_key", existing_type=sa.Text(), nullable=False)
    op.create_index("ix_work_sort_key_id", "work", ["sort_key", "id"])


def downgrade() -> None:
    op.drop_index("ix_work_sort_key_id", table_name="work")
    with op.batch_alter_table("work") as batch:
        batch.drop_column("sort_key")
    op.create_index("ix_work_sort_title_id", "work", ["sort_title", "id"])
