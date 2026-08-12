from datetime import date, datetime

from sqlalchemy import ForeignKey, Index, UniqueConstraint, false, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ludarium.enums import ItemKind
from ludarium.models.base import Base
from ludarium.models.types import enum_column, utcnow


class Work(Base):
    """The canonical title, IGDB-anchored once matched.

    Every column here holds a **resolved** value. Providers write
    `field_provenance` rows and the resolver writes here (rule 9), so a sync
    that goes wrong can at worst add a losing provenance row.

    Every entitlement has a work from the moment it is synced: a new one gets a
    stub with `is_matched = false` and `title` copied from `provider_title`
    (ADR-0015).
    """

    __tablename__ = "work"
    __table_args__ = (
        # Keyset pagination for the virtualised grid. The M3 filter indexes are
        # deliberately absent until there is a query to size them against.
        Index("ix_work_sort_title_id", "sort_title", "id"),
        Index(
            "uq_work_igdb_id",
            "igdb_id",
            unique=True,
            sqlite_where=text("igdb_id IS NOT NULL"),
            postgresql_where=text("igdb_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    # Display logic, ours: the leading article moved. See `ludarium.titles`.
    sort_title: Mapped[str]
    # Matcher logic, `ludamatch`'s (MIT, M2). Nullable because nothing in M1
    # writes it, and writing it here would put matcher code in the wrong repo.
    normalised_title: Mapped[str | None]
    item_kind: Mapped[ItemKind] = mapped_column(
        enum_column(ItemKind, "item_kind"),
        default=ItemKind.GAME,
        server_default=text(f"'{ItemKind.GAME.value}'"),
    )
    # DLC folded under its parent game in the grid (M3).
    parent_work_id: Mapped[int | None] = mapped_column(ForeignKey("work.id", ondelete="RESTRICT"))
    # Year separately from the date: day-level precision is noise for filtering.
    release_year: Mapped[int | None]
    release_date: Mapped[date | None]
    summary: Mapped[str | None]
    metacritic_score: Mapped[int | None]
    # Required wherever the score is displayed: RAWG asks for an active link.
    metacritic_url: Mapped[str | None]
    # Denormalised for lookups; the authoritative copy lives in `external_id`.
    igdb_id: Mapped[int | None]
    is_matched: Mapped[bool] = mapped_column(default=False, server_default=false())
    enriched_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    parent_work: Mapped["Work | None"] = relationship(remote_side=[id], lazy="raise_on_sql")


class Edition(Base):
    """Differs from its work in bundled content, not in identity.

    Every work has at least one — a `Standard` stub — so a provider entry with
    no edition information has something to attach to.
    """

    __tablename__ = "edition"
    __table_args__ = (UniqueConstraint("work_id", "slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("work.id", ondelete="CASCADE"))
    name: Mapped[str]
    slug: Mapped[str]
    is_default: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    work: Mapped[Work] = relationship(lazy="raise_on_sql")
