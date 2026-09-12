from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from ludarium.models.base import Base
from ludarium.models.types import UpdatedAt


class TwitchAppToken(Base):
    """The app access token IGDB authenticates with, kept across restarts.

    Keyed by the client id it was minted for, so a changed application reads as
    a miss instead of presenting the old one's token. Fernet ciphertext, like
    `account.credentials_encrypted`: a token that expires in weeks still works
    today, which makes it a credential (rule 7).
    """

    __tablename__ = "twitch_app_token"

    client_id: Mapped[str] = mapped_column(primary_key=True)
    access_token_encrypted: Mapped[bytes]
    expires_at: Mapped[datetime]
    updated_at: Mapped[UpdatedAt]
