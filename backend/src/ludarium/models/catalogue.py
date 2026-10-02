from datetime import date, datetime

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from ludarium import titles
from ludarium.enums import CompanyRole, EntityType, ImageKind, ItemKind, SteamRating
from ludarium.models.base import Base
from ludarium.models.types import CreatedAt, UpdatedAt, enum_column


def _ordered_by(name: str, value: str) -> tuple[Index, Index]:
    """The two indexes a listing order over `value` seeks on, one per direction.

    Shaped as `ludarium.sorting` orders: nulls last, then the value, then the
    title ascending in either direction. A row-value index read backwards would
    put the nulls first and the titles Z to A, so each direction has its own.
    `value` is a column or an expression, written as `sorting` writes it: SQLite
    uses an expression index only for the same expression.
    """

    # Parenthesised only when it is an expression, so the plain columns keep the
    # DDL their migration wrote.
    grouped = f"({value})" if " " in value else value
    return (
        Index(f"ix_work_{name}_asc", text(f"{grouped} IS NULL"), text(value), "sort_key", "id"),
        Index(
            f"ix_work_{name}_desc",
            text(f"{grouped} IS NULL"),
            text(f"{grouped} DESC"),
            "sort_key",
            "id",
        ),
    )


# `scores.steam_score` at the default threshold: the percentage over at least
# ten reviews. Another threshold is another expression, and sorts (#108).
STEAM_SCORE = "CASE WHEN steam_review_count >= 10 THEN steam_review_percent END"


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
        # Keyset pagination for the virtualised grid, on the folded key rather
        # than on `sort_title` (ADR-0018). There are no filter indexes, and that
        # was measured rather than assumed (#90): the listing walks this index
        # and stops at a page, so on 20,000 works a filter answers in about
        # 1 ms, and one that matches nothing in 8. An index on `item_kind` or
        # `release_year` made the planner seek and then sort, which took 3 ms.
        Index("ix_work_sort_key_id", "sort_key", "id"),
        # The other orders over `work`'s own columns (#94). Without them each
        # page sorted every work: 42 ms for the first page of 20,000, measured,
        # against 1.1 ms with them at any depth. Playtime and last played have
        # none, and cannot use one: they are on `user_work_state`, which the
        # listing outer-joins, and SQLite does not drive a LEFT JOIN from its
        # right side. They sort every page, 37 ms at 20,000 works.
        *_ordered_by("metacritic_score", "metacritic_score"),
        *_ordered_by("steam_review_score", STEAM_SCORE),
        *_ordered_by("release_date", "release_date"),
        # The add-ons of a work: counted on each card, and searched through
        # while they are folded under it (#98).
        Index(
            "ix_work_parent_work_id",
            "parent_work_id",
            sqlite_where=text("parent_work_id IS NOT NULL"),
            postgresql_where=text("parent_work_id IS NOT NULL"),
        ),
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
    # Resolved, so a user's own spelling of it survives a sync (rule 3).
    sort_title: Mapped[str]
    # `sort_title` as the database compares it, and never assigned directly: the
    # validator below keeps it in step with every assignment to `sort_title`,
    # the resolver's included. A bulk `UPDATE` would bypass it and leave the key
    # stale until startup repairs it — nothing issues one against `work`, and a
    # resolver test holds the write path to the ORM.
    #
    # `COLLATE "C"` on PostgreSQL, where the default is the database's locale and
    # a locale collation reorders punctuation and spaces by its own rules. `C` is
    # byte order, which on UTF-8 is code point order — what SQLite's `BINARY` and
    # Python's `sorted` already do, so the three agree (ADR-0018).
    sort_key: Mapped[str] = mapped_column(Text().with_variant(Text(collation="C"), "postgresql"))
    # Matcher logic, `ludamatch`'s (MIT, M2). Nullable because nothing in M1
    # writes it, and writing it here would put matcher code in the wrong repo.
    normalised_title: Mapped[str | None]
    # `title` folded for search, kept in step by the validator below as
    # `sort_key` is, and rewritten at startup if the fold has moved (#53).
    title_key: Mapped[str]
    # Null until a source has classified the work (#41). A default of `game`
    # made "nobody has looked" and "a game" the same value, and the matcher
    # must be able to skip the first: a playtest is not what a stub looks like.
    item_kind: Mapped[ItemKind | None] = mapped_column(enum_column(ItemKind, "item_kind"))
    # DLC folded under its parent game in the grid (M3).
    parent_work_id: Mapped[int | None] = mapped_column(ForeignKey("work.id", ondelete="RESTRICT"))
    # Year separately from the date: day-level precision is noise for filtering.
    release_year: Mapped[int | None]
    release_date: Mapped[date | None]
    summary: Mapped[str | None]
    metacritic_score: Mapped[int | None]
    # Required wherever the score is displayed: RAWG asks for an active link.
    metacritic_url: Mapped[str | None]
    # The Steam store's summary of one of the work's apps, the four written
    # together so that they always describe the same app (ADR-0027).
    steam_review_rating: Mapped[SteamRating | None] = mapped_column(
        enum_column(SteamRating, "steam_rating")
    )
    steam_review_percent: Mapped[int | None]
    steam_review_count: Mapped[int | None]
    steam_review_appid: Mapped[str | None]
    # Denormalised for lookups; the authoritative copy lives in `external_id`.
    igdb_id: Mapped[int | None]
    is_matched: Mapped[bool] = mapped_column(default=False, server_default=false())
    enriched_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]

    parent_work: Mapped["Work | None"] = relationship(remote_side=[id], lazy="raise_on_sql")

    @validates("sort_title")
    def _keep_the_sort_key_in_step(self, _field: str, value: str) -> str:
        self.sort_key = titles.sort_key(value)
        return value

    @validates("title")
    def _keep_the_title_key_in_step(self, _field: str, value: str) -> str:
        self.title_key = titles.search_key(value)
        return value


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
    created_at: Mapped[CreatedAt]

    work: Mapped[Work] = relationship(lazy="raise_on_sql")


class ExternalId(Base):
    """An id another system gives a work or an edition: the backbone of matching layer 1.

    The one place such an id is authoritative. `work.igdb_id` is a copy of the
    `igdb` row kept for lookups, and never the other way round (ADR-0021).
    """

    __tablename__ = "external_id"
    __table_args__ = (
        # One owner per id: IGDB game 1942 is one work, whatever else claims it.
        # A second claimant is a merge waiting to happen, not a second row.
        UniqueConstraint("namespace", "value", "entity_type"),
        # The other direction: every id an entity carries.
        Index("ix_external_id_entity_type_entity_id", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Polymorphic like `field_provenance`, so no foreign key.
    entity_type: Mapped[EntityType] = mapped_column(enum_column(EntityType, "entity_type"))
    entity_id: Mapped[int]
    # `igdb`, `steam`, `gog`, `epic`, `rawg`, `wikidata`.
    namespace: Mapped[str]
    # Text, because the ids are: an Epic id is 32 hex characters.
    value: Mapped[str]
    # True when IGDB `external_games` said so, false when a matcher inferred it.
    is_authoritative: Mapped[bool] = mapped_column(default=False, server_default=false())
    # The provider key that asserted it.
    source_ref: Mapped[str | None]
    created_at: Mapped[CreatedAt]


class ImageAsset(Base):
    """One image file of a work, on disk under the data directory (ADR-0019, ADR-0024).

    The file, not the picture: a cover fetched at two sizes is two rows, told
    apart by `width`, so each has its own checksum and path. A changed cover is
    new rows rather than rewritten ones, and the served URL names the row — so
    a browser can keep an image for good without ever showing a stale one.
    """

    __tablename__ = "image_asset"
    __table_args__ = (
        Index("ix_image_asset_entity_type_entity_id", "entity_type", "entity_id"),
        # Never a reused id. SQLite hands a freed highest rowid to the next
        # insert, and replacing a work's cover frees exactly those: the new
        # file would be served under the old URL, which browsers were told to
        # keep for good. PostgreSQL's sequences never go back anyway.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Polymorphic like `external_id`, so no foreign key.
    entity_type: Mapped[EntityType] = mapped_column(enum_column(EntityType, "entity_type"))
    entity_id: Mapped[int]
    kind: Mapped[ImageKind] = mapped_column(enum_column(ImageKind, "image_kind"))
    # The provider key. Also what decides whether the file may leave the
    # instance: IGDB's may not (M5 exports).
    source_ref: Mapped[str]
    remote_url: Mapped[str | None]
    # Relative to the data directory, and only ever written by us.
    local_path: Mapped[str | None]
    # SHA-256 of the file, hex.
    checksum: Mapped[str | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    # Null means queued, not yet downloaded.
    fetched_at: Mapped[datetime | None]


class Company(Base):
    """A publisher, developer, porter or support studio, as IGDB knows it (#82)."""

    __tablename__ = "company"
    __table_args__ = (
        Index(
            "uq_company_igdb_id",
            "igdb_id",
            unique=True,
            sqlite_where=text("igdb_id IS NOT NULL"),
            postgresql_where=text("igdb_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    # The matcher's, like `work.normalised_title`: `ludamatch` writes it, and
    # nothing here does, so it is null until the matcher's publisher feature.
    normalised_name: Mapped[str | None]
    igdb_id: Mapped[int | None]


class WorkCompany(Base):
    """Who made or published a work, in which role."""

    __tablename__ = "work_company"
    __table_args__ = (
        Index("ix_work_company_company_id_work_id_role", "company_id", "work_id", "role"),
    )

    work_id: Mapped[int] = mapped_column(
        ForeignKey("work.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[CompanyRole] = mapped_column(
        enum_column(CompanyRole, "company_role"), primary_key=True
    )
    # The provider that asserted the link, so a step replacing its own links
    # leaves anyone else's alone.
    source_ref: Mapped[str | None]


class Genre(Base):
    """A genre as IGDB names it, in English: the UI translates it where it can (#92)."""

    __tablename__ = "genre"

    id: Mapped[int] = mapped_column(primary_key=True)
    # IGDB's slug, which is the genre's identity here and the filter's value:
    # stable where the display name may be reworded.
    slug: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]


class WorkGenre(Base):
    """A genre a source says a work belongs to."""

    __tablename__ = "work_genre"

    work_id: Mapped[int] = mapped_column(
        ForeignKey("work.id", ondelete="CASCADE"), primary_key=True
    )
    genre_id: Mapped[int] = mapped_column(
        ForeignKey("genre.id", ondelete="RESTRICT"), primary_key=True
    )
    # The provider that asserted it, so a step replacing its own genres leaves
    # anyone else's alone, as `work_company.source_ref` does.
    source_ref: Mapped[str | None]
