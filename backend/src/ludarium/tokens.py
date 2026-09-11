"""Twitch app access tokens, kept in the database so a restart does not mint one.

A convenience rather than a necessity: a token costs one request to replace, and
Twitch documents no limit on issuing them. It is kept because keeping it costs
nothing new — the ciphertext sits beside the platform credentials the instance
already stores, under the same key (rule 7).
"""

from ludarium.crypto import CredentialCipher, CredentialDecryptionError
from ludarium.db import Database
from ludarium.models import TwitchAppToken
from ludarium.providers.igdb import AppToken


class DatabaseTokenStore:
    """A `TokenStore` over the `twitch_app_token` table."""

    def __init__(self, database: Database, cipher: CredentialCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def load(self, client_id: str) -> AppToken | None:
        async with self._database.reading_session_factory() as session:
            row = await session.get(TwitchAppToken, client_id)
        if row is None:
            return None
        try:
            access_token = self._cipher.decrypt(row.access_token_encrypted)
        except CredentialDecryptionError:
            # Stored under an encryption key that has since changed. A token is
            # one request to replace, so this is a miss rather than a failure,
            # and the next save overwrites the row with one this key can read.
            return None
        return AppToken(access_token=access_token, expires_at=row.expires_at)

    async def save(self, client_id: str, token: AppToken) -> None:
        # Read and written in one IMMEDIATE transaction, so the check and the
        # write have nothing between them but the transaction (ADR-0017).
        async with self._database.writing_session_factory() as session:
            ciphertext = self._cipher.encrypt(token.access_token)
            row = await session.get(TwitchAppToken, client_id)
            if row is None:
                session.add(
                    TwitchAppToken(
                        client_id=client_id,
                        access_token_encrypted=ciphertext,
                        expires_at=token.expires_at,
                    )
                )
            else:
                row.access_token_encrypted = ciphertext
                row.expires_at = token.expires_at
            await session.commit()
