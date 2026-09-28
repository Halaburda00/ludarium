"""companies and who made each work

Revision ID: bf0a91f7dc86
Revises: d72242621625
Create Date: 2026-09-28 19:10:41.118392

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bf0a91f7dc86"
down_revision: str | Sequence[str] | None = "d72242621625"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out rather than read from `CompanyRole`, so a later value cannot
# reach back and change what this revision created.
ROLES = ("developer", "publisher", "porting", "support")


def upgrade() -> None:
    op.create_table(
        "company",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalised_name", sa.Text(), nullable=True),
        sa.Column("igdb_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company")),
    )
    op.create_index(
        "uq_company_igdb_id",
        "company",
        ["igdb_id"],
        unique=True,
        sqlite_where=sa.text("igdb_id IS NOT NULL"),
        postgresql_where=sa.text("igdb_id IS NOT NULL"),
    )
    op.create_table(
        "work_company",
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(*ROLES, name="company_role", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["work_id"], ["work.id"], name=op.f("fk_work_company_work_id_work"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["company.id"],
            name=op.f("fk_work_company_company_id_company"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("work_id", "company_id", "role", name=op.f("pk_work_company")),
    )
    op.create_index(
        "ix_work_company_company_id_work_id_role",
        "work_company",
        ["company_id", "work_id", "role"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_company_company_id_work_id_role", table_name="work_company")
    op.drop_table("work_company")
    op.drop_index("uq_company_igdb_id", table_name="company")
    op.drop_table("company")
