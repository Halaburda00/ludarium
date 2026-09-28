"""folded titles for search

Revision ID: d72242621625
Revises: 1341c82a2432
Create Date: 2026-09-28 18:02:11.402213

"""

import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d72242621625"
down_revision: str | Sequence[str] | None = "1341c82a2432"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `ludarium.titles.search_key` as it stands at this revision, copied rather
# than imported for the reason `cb303273d67a` gives: a later change to the fold
# must not reach back and change what this backfill wrote. A test holds the copy
# equal to the function today, and startup rewrites any key that later
# disagrees with it.
MARKS = str.maketrans("", "", "™®©℠")

# Each folded column and the title it folds.
KEYS = (("work", "title", "title_key"), ("entitlement", "provider_title", "provider_title_key"))


def search_key(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.translate(MARKS))
    folded = unicodedata.normalize("NFKD", decomposed.casefold())
    bare = "".join(character for character in folded if not unicodedata.combining(character))
    return " ".join(bare.split())


def upgrade() -> None:
    connection = op.get_bind()
    for table, source, key in KEYS:
        # Nullable until it is filled: SQLite adds a NOT NULL column only with a
        # default, and any default would be a key nobody computed.
        op.add_column(table, sa.Column(key, sa.Text(), nullable=True))
        # In Python, because the fold is the one SQL cannot do here (ADR-0018).
        rows = connection.execute(sa.text(f"SELECT id, {source} FROM {table}")).all()
        if rows:
            connection.execute(
                sa.text(f"UPDATE {table} SET {key} = :key WHERE id = :id"),
                [{"id": row_id, "key": search_key(value)} for row_id, value in rows],
            )
        # Batch mode rebuilds the table, which is safe for the reason
        # `cb303273d67a` gives: Alembic's connection never turns `foreign_keys`
        # on, so the tables referencing these with ON DELETE CASCADE keep
        # their rows.
        with op.batch_alter_table(table) as batch:
            batch.alter_column(key, existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    for table, _, key in KEYS:
        with op.batch_alter_table(table) as batch:
            batch.drop_column(key)
