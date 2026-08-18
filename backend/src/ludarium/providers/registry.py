"""Which client answers for which provider key.

The mapping is here rather than in the API layer so that the scheduler and the
future ingest endpoint reach for the same one. Adding GOG in M4 is a line in
`BUILDERS` and a client beside `SteamProvider`, not a new branch in an endpoint.
"""

from collections.abc import Callable
from typing import Final

import httpx

from ludarium.providers.base import LibraryProvider
from ludarium.providers.steam import SteamCredentials, SteamProvider

type Builder = Callable[[str, str, httpx.AsyncClient], LibraryProvider]


class UnsupportedProviderError(Exception):
    """A provider row exists but nothing can sync it.

    `manual` is the standing example: a real provider, with entitlements of its
    own, and nothing to ask for a library.
    """


def _steam(external_account_id: str, secret: str, client: httpx.AsyncClient) -> LibraryProvider:
    # The SteamID64 is the account's public identity and lives in the column;
    # only the Web API key is a secret, so only it is encrypted.
    return SteamProvider(SteamCredentials(api_key=secret, steam_id=external_account_id), client)


BUILDERS: Final[dict[str, Builder]] = {"steam": _steam}


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
