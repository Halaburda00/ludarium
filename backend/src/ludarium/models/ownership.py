from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ludarium.enums import EntitlementOrigin, ItemKind, MatchLayer, OwnershipType, WorkLinkRole
from ludarium.models.base import Base
from ludarium.models.catalogue import Edition, Work
from ludarium.models.provider import Account, SyncRun
from ludarium.models.types import enum_column, utcnow


class Entitlement(Base):
    """What the user owns on one account. This is the row a sync touches."""

    __tablename__ = "entitlement"
    __table_args__ = (
        # The sync upsert key, and what keeps manual rows out of sync's way:
        # they carry no `provider_item_id` and so match nothing (rule 2).
        Index(
            "uq_entitlement_account_id_provider_item_id",
            "account_id",
            "provider_item_id",
            unique=True,
            sqlite_where=text("provider_item_id IS NOT NULL"),
            postgresql_where=text("provider_item_id IS NOT NULL"),
        ),
        Index("ix_entitlement_edition_id", "edition_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), default=1, server_default=text("1")
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id", ondelete="RESTRICT"))
    # Which edition was bought, not the route to the work: that is the `primary`
    # link in `entitlement_work`. Null only between insert and stub creation.
    edition_id: Mapped[int | None] = mapped_column(ForeignKey("edition.id", ondelete="RESTRICT"))
    origin: Mapped[EntitlementOrigin] = mapped_column(
        enum_column(EntitlementOrigin, "entitlement_origin"),
        default=EntitlementOrigin.SYNC,
        server_default=text(f"'{EntitlementOrigin.SYNC.value}'"),
    )
    provider_item_id: Mapped[str | None]
    # Exactly as the platform returned it. Never overwritten by metadata: it is
    # the matcher's input and the fallback display title.
    provider_title: Mapped[str]
    ownership_type: Mapped[OwnershipType] = mapped_column(
        enum_column(OwnershipType, "ownership_type"),
        default=OwnershipType.OWNED,
        server_default=text(f"'{OwnershipType.OWNED.value}'"),
    )
    # As the provider reports it; the resolved value lives on `work`.
    item_kind: Mapped[ItemKind | None] = mapped_column(enum_column(ItemKind, "item_kind"))
    playtime_minutes: Mapped[int | None]
    last_played_at: Mapped[datetime | None]
    installed: Mapped[bool | None]
    install_path: Mapped[str | None]
    acquired_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    # Set when a run no longer sees the item, and only by a run that finished
    # `success`. Never a DELETE (rule 1).
    removed_at: Mapped[datetime | None]
    removed_by_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_run.id", ondelete="RESTRICT")
    )
    # Last provider record, for debugging a bad match. Token fields are stripped
    # before it gets here (rule 7).
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )

    account: Mapped[Account] = relationship(lazy="raise_on_sql")
    edition: Mapped[Edition | None] = relationship(lazy="raise_on_sql")
    removed_by_run: Mapped[SyncRun | None] = relationship(lazy="raise_on_sql")


class EntitlementWork(Base):
    """The many-to-many, because a bundle grants several works at once.

    The reverse holds too: one work is reached by several entitlements when the
    same game is owned on Steam and on GOG.
    """

    __tablename__ = "entitlement_work"
    __table_args__ = (
        # "Exactly one primary per entitlement" is the single source of truth for
        # which work an entitlement belongs to, so it is an index and not a
        # convention.
        Index(
            "uq_entitlement_work_entitlement_id_primary",
            "entitlement_id",
            unique=True,
            sqlite_where=text(f"role = '{WorkLinkRole.PRIMARY.value}'"),
            postgresql_where=text(f"role = '{WorkLinkRole.PRIMARY.value}'"),
        ),
        # Both traversal directions are hot; the PK covers the other one.
        Index("ix_entitlement_work_work_id_entitlement_id", "work_id", "entitlement_id"),
    )

    entitlement_id: Mapped[int] = mapped_column(
        ForeignKey("entitlement.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("work.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[WorkLinkRole] = mapped_column(
        enum_column(WorkLinkRole, "work_link_role"),
        default=WorkLinkRole.PRIMARY,
        server_default=text(f"'{WorkLinkRole.PRIMARY.value}'"),
    )
    match_layer: Mapped[MatchLayer | None] = mapped_column(enum_column(MatchLayer, "match_layer"))
    # Null for hard IDs and manual links, which are not scored.
    confidence: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    created_by_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_run.id", ondelete="RESTRICT")
    )

    entitlement: Mapped[Entitlement] = relationship(lazy="raise_on_sql")
    work: Mapped[Work] = relationship(lazy="raise_on_sql")
