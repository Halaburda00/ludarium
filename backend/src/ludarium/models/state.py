from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ludarium.enums import PlayStatus
from ludarium.models.base import Base
from ludarium.models.catalogue import Work
from ludarium.models.types import CreatedAt, UpdatedAt, enum_column


class UserWorkState(Base):
    """Everything the user decides, plus the aggregates the grid sorts on.

    Separate from `work` so that a metadata refresh cannot touch it: rule 3 is a
    guarantee about user edits, and the cheapest way to keep it is to put them
    in a table no provider writes.
    """

    __tablename__ = "user_work_state"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 10", name="rating_range"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"),
        primary_key=True,
        default=1,
        server_default=text("1"),
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("work.id", ondelete="CASCADE"), primary_key=True
    )
    play_status: Mapped[PlayStatus] = mapped_column(
        enum_column(PlayStatus, "play_status"),
        default=PlayStatus.NOT_STARTED,
        server_default=text(f"'{PlayStatus.NOT_STARTED.value}'"),
    )
    rating: Mapped[int | None]
    notes: Mapped[str | None]
    is_favourite: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Excluded from the default grid without being removed.
    is_hidden: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Aggregate: the **sum** over this work's entitlements. Two accounts are two
    # stretches of play, not two reports of one (rule 5).
    playtime_minutes: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_played_at: Mapped[datetime | None]
    # Denormalised on resolve so "owned on more than one platform" is an indexed
    # comparison rather than an aggregate per row.
    platform_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    updated_at: Mapped[UpdatedAt]

    work: Mapped[Work] = relationship(lazy="raise_on_sql")


class SavedView(Base):
    """A named library query: filters and an order, never the works they matched.

    Stored as the query string the listing takes, and read back through the
    same filter registry as a live request, so a view is re-run each time it is
    opened and follows the library as it changes.
    """

    __tablename__ = "saved_view"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), default=1, server_default=text("1")
    )
    name: Mapped[str]
    query: Mapped[str]
    # Not unique: a reorder rewrites every row, and a unique position would
    # collide with itself halfway through. Ties list by `id`.
    position: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
