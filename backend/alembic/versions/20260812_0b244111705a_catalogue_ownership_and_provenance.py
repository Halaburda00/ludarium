"""catalogue, ownership and provenance

Revision ID: 0b244111705a
Revises: eeac668d07ff
Create Date: 2026-08-12 18:21:14.123085

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0b244111705a"
down_revision: str | Sequence[str] | None = "eeac668d07ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Autogenerate proposed dropping the enum CHECKs and rebuilding the
    # expression indexes from the previous revision. Both are artefacts of what
    # SQLite can reflect, not real drift, and both were removed by hand.
    op.create_table(
        "work",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("sort_title", sa.Text(), nullable=False),
        sa.Column("normalised_title", sa.Text(), nullable=True),
        sa.Column(
            "item_kind",
            sa.Enum(
                "game",
                "dlc",
                "demo",
                "soundtrack",
                "video",
                "tool",
                "mod",
                name="item_kind",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'game'"),
            nullable=False,
        ),
        sa.Column("parent_work_id", sa.Integer(), nullable=True),
        sa.Column("release_year", sa.Integer(), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("metacritic_score", sa.Integer(), nullable=True),
        sa.Column("metacritic_url", sa.Text(), nullable=True),
        sa.Column("igdb_id", sa.Integer(), nullable=True),
        sa.Column("is_matched", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["parent_work_id"],
            ["work.id"],
            name=op.f("fk_work_parent_work_id_work"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work")),
    )
    op.create_index("ix_work_sort_title_id", "work", ["sort_title", "id"])
    op.create_index(
        "uq_work_igdb_id",
        "work",
        ["igdb_id"],
        unique=True,
        sqlite_where=sa.text("igdb_id IS NOT NULL"),
        postgresql_where=sa.text("igdb_id IS NOT NULL"),
    )
    op.create_table(
        "edition",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["work_id"], ["work.id"], name=op.f("fk_edition_work_id_work"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edition")),
        sa.UniqueConstraint("work_id", "slug", name=op.f("uq_edition_work_id_slug")),
    )
    op.create_table(
        "user_work_state",
        sa.Column("user_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column(
            "play_status",
            sa.Enum(
                "not_started",
                "playing",
                "completed",
                "mastered",
                "dropped",
                "on_hold",
                "wishlist",
                name="play_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'not_started'"),
            nullable=False,
        ),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_favourite", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_hidden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("playtime_minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("platform_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 10", name=op.f("ck_user_work_state_rating_range")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_user_work_state_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["work.id"],
            name=op.f("fk_user_work_state_work_id_work"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "work_id", name=op.f("pk_user_work_state")),
    )
    op.create_table(
        "entitlement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("edition_id", sa.Integer(), nullable=True),
        sa.Column(
            "origin",
            sa.Enum(
                "sync",
                "manual",
                "import",
                "agent",
                name="entitlement_origin",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'sync'"),
            nullable=False,
        ),
        sa.Column("provider_item_id", sa.Text(), nullable=True),
        sa.Column("provider_title", sa.Text(), nullable=False),
        sa.Column(
            "ownership_type",
            sa.Enum(
                "owned",
                "subscription",
                "free",
                "family_shared",
                "trial",
                "physical",
                name="ownership_type",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'owned'"),
            nullable=False,
        ),
        sa.Column(
            "item_kind",
            sa.Enum(
                "game",
                "dlc",
                "demo",
                "soundtrack",
                "video",
                "tool",
                "mod",
                name="item_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("playtime_minutes", sa.Integer(), nullable=True),
        sa.Column("last_played_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("installed", sa.Boolean(), nullable=True),
        sa.Column("install_path", sa.Text(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by_run_id", sa.Integer(), nullable=True),
        sa.Column(
            "raw_payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_entitlement_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name=op.f("fk_entitlement_account_id_account"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["edition.id"],
            name=op.f("fk_entitlement_edition_id_edition"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["removed_by_run_id"],
            ["sync_run.id"],
            name=op.f("fk_entitlement_removed_by_run_id_sync_run"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entitlement")),
    )
    op.create_index(
        "uq_entitlement_account_id_provider_item_id",
        "entitlement",
        ["account_id", "provider_item_id"],
        unique=True,
        sqlite_where=sa.text("provider_item_id IS NOT NULL"),
        postgresql_where=sa.text("provider_item_id IS NOT NULL"),
    )
    op.create_index("ix_entitlement_edition_id", "entitlement", ["edition_id"])
    op.create_table(
        "field_provenance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "entity_type",
            sa.Enum(
                "work",
                "edition",
                "entitlement",
                "account",
                name="entity_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column(
            "source_kind",
            sa.Enum(
                "manual",
                "platform_api",
                "local_agent",
                "metadata_provider",
                name="source_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("is_effective", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sync_run.id"],
            name=op.f("fk_field_provenance_run_id_sync_run"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_field_provenance")),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "field",
            "source_kind",
            "source_ref",
            name=op.f("uq_field_provenance_entity_type_entity_id_field_source_kind_source_ref"),
        ),
    )
    op.create_index(
        "uq_field_provenance_entity_type_entity_id_field_effective",
        "field_provenance",
        ["entity_type", "entity_id", "field"],
        unique=True,
        sqlite_where=sa.text("is_effective"),
        postgresql_where=sa.text("is_effective"),
    )
    op.create_table(
        "entitlement_work",
        sa.Column("entitlement_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "primary",
                "granted",
                name="work_link_role",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'primary'"),
            nullable=False,
        ),
        sa.Column(
            "match_layer",
            sa.Enum(
                "hard_id",
                "alias",
                "fuzzy",
                "llm",
                "manual",
                name="match_layer",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by_run_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["entitlement_id"],
            ["entitlement.id"],
            name=op.f("fk_entitlement_work_entitlement_id_entitlement"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["work.id"],
            name=op.f("fk_entitlement_work_work_id_work"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_run_id"],
            ["sync_run.id"],
            name=op.f("fk_entitlement_work_created_by_run_id_sync_run"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("entitlement_id", "work_id", name=op.f("pk_entitlement_work")),
    )
    op.create_index(
        "uq_entitlement_work_entitlement_id_primary",
        "entitlement_work",
        ["entitlement_id"],
        unique=True,
        sqlite_where=sa.text("role = 'primary'"),
        postgresql_where=sa.text("role = 'primary'"),
    )
    op.create_index(
        "ix_entitlement_work_work_id_entitlement_id",
        "entitlement_work",
        ["work_id", "entitlement_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_entitlement_work_work_id_entitlement_id", table_name="entitlement_work")
    op.drop_index(
        "uq_entitlement_work_entitlement_id_primary",
        table_name="entitlement_work",
        sqlite_where=sa.text("role = 'primary'"),
        postgresql_where=sa.text("role = 'primary'"),
    )
    op.drop_table("entitlement_work")
    op.drop_index(
        "uq_field_provenance_entity_type_entity_id_field_effective",
        table_name="field_provenance",
        sqlite_where=sa.text("is_effective"),
        postgresql_where=sa.text("is_effective"),
    )
    op.drop_table("field_provenance")
    op.drop_index("ix_entitlement_edition_id", table_name="entitlement")
    op.drop_index(
        "uq_entitlement_account_id_provider_item_id",
        table_name="entitlement",
        sqlite_where=sa.text("provider_item_id IS NOT NULL"),
        postgresql_where=sa.text("provider_item_id IS NOT NULL"),
    )
    op.drop_table("entitlement")
    op.drop_table("user_work_state")
    op.drop_table("edition")
    op.drop_index(
        "uq_work_igdb_id",
        table_name="work",
        sqlite_where=sa.text("igdb_id IS NOT NULL"),
        postgresql_where=sa.text("igdb_id IS NOT NULL"),
    )
    op.drop_index("ix_work_sort_title_id", table_name="work")
    op.drop_table("work")
