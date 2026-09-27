"""Which client answers for which provider key.

The mapping is here rather than in the API layer so that the scheduler and the
future ingest endpoint reach for the same one. Adding GOG in M4 is a line in
`BUILDERS` and a client beside `SteamProvider`, not a new branch in an endpoint.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import httpx

from ludarium.providers.base import LibraryProvider
from ludarium.providers.epic import EpicProvider, sign_in
from ludarium.providers.steam import SteamCredentials, SteamProvider

type Builder = Callable[[str, str, httpx.AsyncClient], LibraryProvider]


@dataclass(frozen=True, slots=True, repr=False)
class Connected:
    """What connecting an account settles: whose account it is, and the secret to keep.

    Not always what the user typed. Steam's key is kept as given; Epic's
    authorization code is spent on the way in, and what is kept is the refresh
    token it bought, under the account id Epic named.
    """

    external_account_id: str
    secret: str

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(external_account_id={self.external_account_id!r}, secret=***)"
        )


type Connector = Callable[[str, str, httpx.AsyncClient], Awaitable[Connected]]


class UnsupportedProviderError(Exception):
    """A provider row exists but nothing can sync it.

    `manual` is the standing example: a real provider, with entitlements of its
    own, and nothing to ask for a library.
    """


def _steam(external_account_id: str, secret: str, client: httpx.AsyncClient) -> LibraryProvider:
    # The SteamID64 is the account's public identity and lives in the column;
    # only the Web API key is a secret, so only it is encrypted.
    return SteamProvider(SteamCredentials(api_key=secret, steam_id=external_account_id), client)


def _epic(external_account_id: str, secret: str, client: httpx.AsyncClient) -> LibraryProvider:
    return EpicProvider(secret, external_account_id, client)


async def _connect_by_key(
    key: str, external_account_id: str, secret: str, client: httpx.AsyncClient
) -> Connected:
    await build_library(
        key, external_account_id=external_account_id, secret=secret, client=client
    ).validate_credentials()
    return Connected(external_account_id, secret)


async def _connect_steam(
    external_account_id: str, secret: str, client: httpx.AsyncClient
) -> Connected:
    return await _connect_by_key("steam", external_account_id, secret, client)


async def _connect_epic(_: str, code: str, client: httpx.AsyncClient) -> Connected:
    # Signing in is the validation: a code Epic accepts is an account it
    # vouches for, and its id comes from Epic rather than from the form.
    signed = await sign_in(client, code)
    return Connected(signed.account_id, signed.refresh_token)


BUILDERS: Final[dict[str, Builder]] = {"steam": _steam, "epic": _epic}
CONNECTORS: Final[dict[str, Connector]] = {"steam": _connect_steam, "epic": _connect_epic}


def supports(key: str) -> bool:
    """Whether this provider can be asked for a library at all.

    Asked before a credential is decrypted, so that "manual cannot be synced" is
    answered as itself rather than as whatever the decryption made of a column
    that was never a Fernet token.
    """

    return key in BUILDERS


def build_library(
    key: str, *, external_account_id: str, secret: str, client: httpx.AsyncClient
) -> LibraryProvider:
    builder = BUILDERS.get(key)
    if builder is None:
        raise UnsupportedProviderError(f"`{key}` has no library client")
    return builder(external_account_id, secret, client)


async def connect(
    key: str, *, external_account_id: str, credential: str, client: httpx.AsyncClient
) -> Connected:
    """Check what the user handed over with the platform and settle what to store."""

    connector = CONNECTORS.get(key)
    if connector is None:
        raise UnsupportedProviderError(f"`{key}` has no library client")
    return await connector(external_account_id, credential, client)
