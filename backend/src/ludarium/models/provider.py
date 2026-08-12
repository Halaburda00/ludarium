from datetime import datetime

from sqlalchemy import ForeignKey, Index, false, func, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ludarium.enums import LicenceClass, ProviderKind, SourceKind, SyncStatus, SyncTrigger
from ludarium.models.base import Base
from ludarium.models.types import enum_column, utcnow


class Provider(Base):
    """Seeded from code, not user-created.

    `manual` is a provider row like any platform, and the local agent and the
    metadata providers will be too, so that every entitlement has a source and
    no foreign key needs to be nullable.
    """

    __tablename__ = "provider"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[ProviderKind] = mapped_column(enum_column(ProviderKind, "provider_kind"))
    source_kind: Mapped[SourceKind] = mapped_column(enum_column(SourceKind, "source_kind"))
    licence_class: Mapped[LicenceClass] = mapped_column(
        enum_column(LicenceClass, "licence_class"),
        default=LicenceClass.REDISTRIBUTABLE,
        server_default=text(f"'{LicenceClass.REDISTRIBUTABLE.value}'"),
    )
    display_name: Mapped[str]
    attribution_html: Mapped[str | None]
    store_url_template: Mapped[str | None]
    precedence_weight: Mapped[int] = mapped_column(default=100, server_default=text("100"))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    status: Mapped[SyncStatus] = mapped_column(
        enum_column(SyncStatus, "sync_status"),
        default=SyncStatus.PENDING,
        server_default=text(f"'{SyncStatus.PENDING.value}'"),
    )
    last_success_at: Mapped[datetime | None]
    # Message only, never a payload that could hold a token (rule 7).
    last_error: Mapped[str | None]


class Account(Base):
    """A connected account on a platform. Several per provider are allowed (M4)."""

    __tablename__ = "account"
    __table_args__ = (
        # Partial, because `manual` and every derived account leave
        # `external_account_id` null and must not collide with each other.
        Index(
            "uq_account_provider_id_external_account_id",
            "provider_id",
            "external_account_id",
            unique=True,
            sqlite_where=text("external_account_id IS NOT NULL"),
            postgresql_where=text("external_account_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), default=1, server_default=text("1")
    )
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id", ondelete="RESTRICT"))
    external_account_id: Mapped[str | None]
    label: Mapped[str]
    # Discovered inside an import rather than connected by the user: no
    # credentials, never synced directly.
    is_derived: Mapped[bool] = mapped_column(default=False, server_default=false())
    credentials_encrypted: Mapped[bytes | None]
    credentials_updated_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    last_success_at: Mapped[datetime | None]

    provider: Mapped[Provider] = relationship(lazy="raise_on_sql")


class SyncRun(Base):
    """One row per attempt, per provider, per account.

    The ingest endpoint (rule 8) writes the same shape with `trigger = ingest`,
    so a remote provider, the local agent and a manual upload are
    indistinguishable downstream.
    """

    __tablename__ = "sync_run"
    __table_args__ = (
        Index("ix_sync_run_provider_id_started_at", "provider_id", text("started_at DESC")),
        Index(
            "ix_sync_run_account_id_status_started_at",
            "account_id",
            "status",
            text("started_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id", ondelete="RESTRICT"))
    # Null for metadata providers, which sync no account.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id", ondelete="RESTRICT"))
    trigger: Mapped[SyncTrigger] = mapped_column(enum_column(SyncTrigger, "sync_trigger"))
    status: Mapped[SyncStatus] = mapped_column(
        enum_column(SyncStatus, "sync_status"),
        default=SyncStatus.RUNNING,
        server_default=text(f"'{SyncStatus.RUNNING.value}'"),
    )
    started_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    finished_at: Mapped[datetime | None]
    items_seen: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    items_added: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    items_updated: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Marked with `removed_at`, never deleted (rule 1).
    items_removed: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    error_text: Mapped[str | None]

    provider: Mapped[Provider] = relationship(lazy="raise_on_sql")
    account: Mapped[Account | None] = relationship(lazy="raise_on_sql")
