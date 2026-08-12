from datetime import datetime

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ludarium.models.base import Base
from ludarium.models.types import CreatedAt


class AppUser(Base):
    """One row in practice. It exists so that `user_id` points at something real."""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    locale: Mapped[str] = mapped_column(default="en", server_default=text("'en'"))
    created_at: Mapped[CreatedAt]
    last_login_at: Mapped[datetime | None]


class UserSession(Base):
    """A login session. Named `UserSession` so it never reads as a SQLAlchemy session.

    Narrow on purpose: `user_agent` and the "sign out other devices" view it
    exists for arrive in M5, when someone other than the author hosts this.
    """

    __tablename__ = "session"
    __table_args__ = (Index("ix_session_expires_at", "expires_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    token_hash: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[CreatedAt]
    expires_at: Mapped[datetime]

    # raise_on_sql: an implicit lazy load under asyncio fails as MissingGreenlet
    # somewhere unrelated. Loading is explicit here or it is a bug.
    user: Mapped[AppUser] = relationship(lazy="raise_on_sql")
