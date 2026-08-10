"""identity, provider and sync run

Revision ID: eeac668d07ff
Revises:
Create Date: 2026-08-11 00:36:19.762422

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eeac668d07ff"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Column and constraint order follows the model declarations: a test compares
    # the DDL this produces with the DDL the models produce, and that comparison
    # is only cheap while the two are textually identical.
    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), server_default=sa.text("'en'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("username", name=op.f("uq_app_user_username")),
    )
    op.create_table(
        "provider",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "platform",
                "metadata",
                "agent",
                "manual",
                name="provider_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
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
        sa.Column(
            "licence_class",
            sa.Enum(
                "redistributable",
                "runtime_only",
                name="licence_class",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'redistributable'"),
            nullable=False,
        ),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("attribution_html", sa.Text(), nullable=True),
        sa.Column("store_url_template", sa.Text(), nullable=True),
        sa.Column("precedence_weight", sa.Integer(), server_default=sa.text("100"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "success",
                "partial",
                "failed",
                name="sync_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider")),
        sa.UniqueConstraint("key", name=op.f("uq_provider_key")),
    )
    op.create_table(
        "account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("external_account_id", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("is_derived", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("credentials_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("credentials_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_account_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name=op.f("fk_account_provider_id_provider"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account")),
    )
    op.create_index(
        "uq_account_provider_id_external_account_id",
        "account",
        ["provider_id", "external_account_id"],
        unique=True,
        sqlite_where=sa.text("external_account_id IS NOT NULL"),
        postgresql_where=sa.text("external_account_id IS NOT NULL"),
    )
    op.create_table(
        "session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_session_user_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_session_token_hash")),
    )
    op.create_index("ix_session_expires_at", "session", ["expires_at"], unique=False)
    op.create_table(
        "sync_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column(
            "trigger",
            sa.Enum(
                "manual",
                "scheduled",
                "ingest",
                "import",
                name="sync_trigger",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "success",
                "partial",
                "failed",
                name="sync_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_added", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_updated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("items_removed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name=op.f("fk_sync_run_provider_id_provider"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name=op.f("fk_sync_run_account_id_account"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_run")),
    )
    # Written by hand: autogenerate skips expression-based indexes because
    # SQLite cannot reflect them.
    op.create_index(
        "ix_sync_run_provider_id_started_at",
        "sync_run",
        ["provider_id", sa.text("started_at DESC")],
    )
    op.create_index(
        "ix_sync_run_account_id_status_started_at",
        "sync_run",
        ["account_id", "status", sa.text("started_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_sync_run_account_id_status_started_at", table_name="sync_run")
    op.drop_index("ix_sync_run_provider_id_started_at", table_name="sync_run")
    op.drop_table("sync_run")
    op.drop_index("ix_session_expires_at", table_name="session")
    op.drop_table("session")
    op.drop_index(
        "uq_account_provider_id_external_account_id",
        table_name="account",
        sqlite_where=sa.text("external_account_id IS NOT NULL"),
        postgresql_where=sa.text("external_account_id IS NOT NULL"),
    )
    op.drop_table("account")
    op.drop_table("provider")
    op.drop_table("app_user")
