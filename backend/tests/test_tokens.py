from datetime import UTC, datetime, timedelta

import httpx
import respx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.crypto import CredentialCipher
from ludarium.db import Database
from ludarium.models import TwitchAppToken
from ludarium.providers import AppToken, IgdbClient, IgdbCredentials
from ludarium.providers import igdb as igdb_module
from ludarium.tokens import DatabaseTokenStore

CLIENT_ID = "not-a-real-client-id"
EXPIRES = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
TOKEN = AppToken("not-a-real-app-access-token", EXPIRES)


def a_cipher() -> CredentialCipher:
    return CredentialCipher(Fernet.generate_key().decode())


async def test_an_application_with_nothing_stored_reads_a_miss(db: Database) -> None:
    assert await DatabaseTokenStore(db, a_cipher()).load(CLIENT_ID) is None


async def test_a_saved_token_is_there_for_the_next_store(db: Database) -> None:
    """What the table is for: a restart builds a new store, and the token is still there."""

    cipher = a_cipher()
    await DatabaseTokenStore(db, cipher).save(CLIENT_ID, TOKEN)

    assert await DatabaseTokenStore(db, cipher).load(CLIENT_ID) == TOKEN


async def test_saving_again_replaces_the_token_rather_than_adding_one(
    db: Database, session: AsyncSession
) -> None:
    store = DatabaseTokenStore(db, a_cipher())
    replacement = AppToken("the-next-one", EXPIRES + timedelta(days=58))

    await store.save(CLIENT_ID, TOKEN)
    await store.save(CLIENT_ID, replacement)

    assert await store.load(CLIENT_ID) == replacement
    assert len(list(await session.scalars(select(TwitchAppToken)))) == 1


async def test_the_token_is_never_stored_in_the_clear(db: Database, session: AsyncSession) -> None:
    await DatabaseTokenStore(db, a_cipher()).save(CLIENT_ID, TOKEN)

    row = (await session.scalars(select(TwitchAppToken))).one()

    assert TOKEN.access_token.encode() not in row.access_token_encrypted


async def test_a_token_the_current_key_cannot_read_is_a_miss(db: Database) -> None:
    """A changed `LUDARIUM_ENCRYPTION_KEY` costs one mint, not a failed enrichment."""

    await DatabaseTokenStore(db, a_cipher()).save(CLIENT_ID, TOKEN)

    assert await DatabaseTokenStore(db, a_cipher()).load(CLIENT_ID) is None


async def test_tokens_are_kept_per_application(db: Database) -> None:
    store = DatabaseTokenStore(db, a_cipher())

    await store.save(CLIENT_ID, TOKEN)

    assert await store.load("the-previous-application") is None


@respx.mock
async def test_a_restarted_client_presents_the_token_the_last_one_minted(db: Database) -> None:
    """The whole point, end to end: two clients, two stores, one database, one mint."""

    cipher = a_cipher()
    credentials = IgdbCredentials(client_id=CLIENT_ID, client_secret="not-a-real-secret")
    minted = respx.post(igdb_module.TWITCH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "not-a-real-app-access-token",
                "expires_in": 5011271,
                "token_type": "bearer",
            },
        )
    )
    route = respx.post(f"{igdb_module.IGDB_API}/popularity_types").mock(
        return_value=httpx.Response(200, json=[])
    )

    for _ in range(2):
        async with httpx.AsyncClient() as client:
            igdb = IgdbClient(credentials, client, tokens=DatabaseTokenStore(db, cipher))
            await igdb.query("popularity_types", "fields name;")

    assert minted.call_count == 1
    assert route.call_count == 2
