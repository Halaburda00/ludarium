from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, false, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ludarium.enums import PlayStatus
from ludarium.models.base import Base
from ludarium.models.catalogue import Work
from ludarium.models.types import enum_column, utcnow


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
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )

    work: Mapped[Work] = relationship(lazy="raise_on_sql")
