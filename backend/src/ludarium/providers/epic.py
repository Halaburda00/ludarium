"""Epic's library, through the launcher's own endpoints (ADR-0025).

Epic has no public library API. What is used here is what the Epic Games
Launcher itself calls, as legendary does: the user signs in on epicgames.com,
pastes back the authorization code the redirect page shows, and the code is
exchanged for a refresh token that is kept, encrypted, as the account's
credential. Every endpoint and field below was measured against the live
service (#64) before anything relied on it; none of it is documented, and it can
change without notice. When it does, this provider fails as itself (rule 4).

The refresh token is replaced on every use. The provider holds the new one in
`renewed_secret` and the sync writes it back when the run closes, success or
not — a spent token left in the column would log the next sync out.

Tokens travel in form bodies and headers and never in a URL, and every error
path keeps them out of messages and chained tracebacks with `raise ... from
None` (rule 7).
"""

import asyncio
import base64
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ludarium.enums import ItemKind, OwnershipType
from ludarium.providers.base import (
    FetchedLibrary,
    InvalidCredentialsError,
    LibraryItem,
    MalformedResponseError,
    ProviderUnavailableError,
    RateLimitedError,
    retry_after_seconds,
)

OAUTH: Final = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
LIBRARY: Final = "https://library-service.live.use1a.on.epicgames.com/library/api/public/items"
CATALOG: Final = "https://catalog-public-service-prod06.ol.epicgames.com/catalog/api/shared"

# The launcher's own client, public in every launcher install and in legendary.
# Not a secret of ours: it identifies the program asking, and the user's own
# sign-in is what grants access.
CLIENT_ID: Final = "34a02cf8f4414e29b15921876da36f9a"
CLIENT_SECRET: Final = "daafbccc737745039dffe53d94fc76cf"  # gitleaks:allow
LOGIN_URL: Final = (
    "https://www.epicgames.com/id/login?redirectUrl=https%3A%2F%2Fwww.epicgames.com%2Fid%2Fapi"
    f"%2Fredirect%3FclientId%3D{CLIENT_ID}%26responseType%3Dcode"
)

# Titles in English, for the matcher, whatever the user's own locale.
CATALOG_CONTEXT: Final = {
    "country": "US",
    "locale": "en",
    "includeDLCDetails": "true",
    "includeMainGameDetails": "true",
}
# Catalogue items asked for per request. The longest namespace in a measured
# library held 60; ids are 32 characters, so fifty keep the URL short.
CATALOG_BATCH: Final = 50
# Catalogue requests in flight. One per namespace, 523 of them for a measured
# library: 58 s one at a time. Epic documents no limit; four is politeness.
AT_ONCE: Final = 4
# A library that has not ended after this many pages is not ending.
MAX_PAGES: Final = 1000
# The catalogue's page of offers, and its largest. 5 of 514 measured namespaces
# held more than 100 offers, the largest 188.
OFFERS_PAGE: Final = 100
# A namespace becomes a path segment. Measured ones are 32 hex characters or a
# codename (`calluna`, `ue`).
NAMESPACE: Final = re.compile(r"[A-Za-z0-9_-]+")
# An app token lasts four hours; it is replaced this long before.
TOKEN_MARGIN_SECONDS: Final = 60.0

# Read at call time so a test can flatten the backoff without waiting for it.
RETRY_ATTEMPTS: Final = 3
RETRY_BACKOFF_SECONDS: Final = 0.5
RETRY_MAX_WAIT_SECONDS: Final = 8.0

# The catalogue's category paths, measured. An item that is none of these —
# an Unreal Marketplace or Fab asset, a plugin, a sample project — is not
# something a game library holds, and is left out rather than guessed at.
DLC_PATHS: Final = frozenset({"addons"})
GAME_PATHS: Final = frozenset({"games"})
TOOL_PATHS: Final = frozenset({"applications", "engines"})


@dataclass(frozen=True, slots=True, repr=False)
class EpicSignIn:
    """What an authorization code is exchanged for: whose account, and the token to keep."""

    account_id: str
    display_name: str | None
    refresh_token: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(account_id={self.account_id!r}, refresh_token=***)"


async def sign_in(client: httpx.AsyncClient, code: str) -> EpicSignIn:
    """Exchange the code the redirect page showed. It is single-use and short-lived."""

    code = code.strip().strip('"')
    if not code:
        raise InvalidCredentialsError("paste the authorization code Epic's page showed")
    payload = await _token(client, {"grant_type": "authorization_code", "code": code})
    return EpicSignIn(
        account_id=_account(payload),
        display_name=payload.get("displayName")
        if isinstance(payload.get("displayName"), str)
        else None,
        refresh_token=_refresh_token(payload),
    )


class EpicProvider:
    """One connected Epic account."""

    key: str = "epic"

    def __init__(self, refresh_token: str, account_id: str, client: httpx.AsyncClient) -> None:
        self._refresh_token = refresh_token
        self._account_id = account_id
        self._client = client
        # Set by the first refresh, and from then on the only valid token.
        self.renewed_secret: str | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(account_id={self._account_id!r}, refresh_token=***)"

    async def validate_credentials(self) -> None:
        await self._access_token()

    async def fetch_library(self) -> FetchedLibrary:
        token = await self._access_token()
        records = _unique(await self._records(token))
        catalog = await self._catalog(token, records)
        items = [_as_item(record, catalog.get(record["catalogItemId"])) for record in records]
        return FetchedLibrary([item for item in items if item is not None])

    async def _access_token(self) -> str:
        payload = await _token(
            self._client,
            {
                "grant_type": "refresh_token",
                "refresh_token": self.renewed_secret or self._refresh_token,
            },
        )
        if _account(payload) != self._account_id:
            # Not ours to reconcile: the stored token belongs to another account
            # than the one this row names, and syncing it would put someone
            # else's library under it.
            raise InvalidCredentialsError("epic answered for a different account than this one")
        self.renewed_secret = _refresh_token(payload)
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise MalformedResponseError("epic's token response has no access_token")
        return access

    async def _records(self, token: str) -> list[dict[str, Any]]:
        """Every page, or an error. A library read short is not a library (rule 1)."""

        records: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            params = [("includeMetadata", "true"), *([("cursor", cursor)] if cursor else [])]
            page = await _get(self._client, LIBRARY, params, token, what="the library")
            found = page.get("records")
            if not isinstance(found, list) or not all(_is_record(record) for record in found):
                raise MalformedResponseError("epic returned a library page without usable records")
            records += found
            metadata = page.get("responseMetadata")
            cursor = metadata.get("nextCursor") if isinstance(metadata, dict) else None
            if not cursor:
                return records
            if not isinstance(cursor, str) or cursor in seen:
                raise MalformedResponseError("epic's library cursor went round in a circle")
            seen.add(cursor)
        raise MalformedResponseError(f"epic's library did not end after {MAX_PAGES} pages")

    async def _catalog(
        self, token: str, records: Sequence[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        by_namespace: dict[str, list[str]] = {}
        for record in records:
            by_namespace.setdefault(record["namespace"], []).append(record["catalogItemId"])
        gate = asyncio.Semaphore(AT_ONCE)

        async def one(namespace: str, ids: Sequence[str]) -> dict[str, Any]:
            async with gate:
                return await _get(
                    self._client,
                    f"{CATALOG}/namespace/{namespace}/bulk/items",
                    [("id", item) for item in ids] + list(CATALOG_CONTEXT.items()),
                    token,
                    what="the catalogue",
                    missing_ok=True,
                )

        batches = [
            (namespace, ids[start : start + CATALOG_BATCH])
            for namespace, ids in sorted(by_namespace.items())
            for start in range(0, len(ids), CATALOG_BATCH)
        ]
        found: dict[str, dict[str, Any]] = {}
        for answer in await asyncio.gather(*(one(namespace, ids) for namespace, ids in batches)):
            found |= {key: item for key, item in answer.items() if isinstance(item, dict)}
        return found


def _is_record(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("namespace"), str)
        and isinstance(record.get("catalogItemId"), str)
        and bool(record["catalogItemId"])
    )


def _unique(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per catalogue item. Epic lists an item once per build it holds."""

    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        unique.setdefault(record["catalogItemId"], record)
    return list(unique.values())


def kind_of(item: Mapping[str, Any] | None) -> ItemKind | None:
    """What the catalogue says an item is, or None where it is not a library item at all."""

    if item is None:
        return None
    paths = {
        category.get("path")
        for category in item.get("categories") or []
        if isinstance(category, dict)
    }
    if paths & DLC_PATHS or item.get("mainGameItem"):
        return ItemKind.DLC
    if paths & GAME_PATHS:
        return ItemKind.GAME
    if paths & TOOL_PATHS:
        return ItemKind.TOOL
    return None


def _as_item(record: Mapping[str, Any], item: Mapping[str, Any] | None) -> LibraryItem | None:
    """The record as a library item, or None for content that is not one.

    An item the catalogue did not answer for keeps its `appName` as a title and
    no kind: owned all the same, and a missing description is no reason to drop
    it — or, worse, to fail a library over it.
    """

    kind = kind_of(item)
    if item is not None and kind is None:
        return None
    title = item.get("title") if item is not None else None
    return LibraryItem(
        provider_item_id=record["catalogItemId"],
        title=title if isinstance(title, str) and title.strip() else str(record.get("appName", "")),
        # Claimed giveaways are owned for good. The catalogue marks no game as
        # free-to-play, so nothing here can be told apart as `free` (#64).
        ownership_type=OwnershipType.OWNED,
        item_kind=kind,
        acquired_at=_moment(record.get("acquisitionDate")),
        raw={
            "namespace": record["namespace"],
            "catalogItemId": record["catalogItemId"],
            "appName": record.get("appName"),
            "productId": record.get("productId"),
            "sandboxName": record.get("sandboxName"),
            "categories": sorted(
                str(category.get("path"))
                for category in (item or {}).get("categories") or []
                if isinstance(category, dict)
            ),
        },
    )


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _account(payload: Mapping[str, Any]) -> str:
    account = payload.get("account_id")
    if not isinstance(account, str) or not account:
        raise MalformedResponseError("epic's token response names no account")
    return account


def _refresh_token(payload: Mapping[str, Any]) -> str:
    token = payload.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise MalformedResponseError("epic's token response has no refresh_token")
    return token


def _retrying() -> AsyncRetrying:
    return AsyncRetrying(
        retry=retry_if_exception_type(ProviderUnavailableError),
        wait=wait_exponential(multiplier=RETRY_BACKOFF_SECONDS, max=RETRY_MAX_WAIT_SECONDS),
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        reraise=True,
    )


async def _token(client: httpx.AsyncClient, data: Mapping[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = await _retrying()(_token_once, client, data)
    return payload


async def _token_once(client: httpx.AsyncClient, data: Mapping[str, str]) -> dict[str, Any]:
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    try:
        response = await client.post(
            OAUTH,
            data={**data, "token_type": "eg1"},
            headers={"Authorization": f"basic {basic}"},
        )
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(f"epic did not answer: {type(exc).__name__}") from None
    status = response.status_code
    if status in (400, 401, 403):
        # `invalid_grant`, measured for a spent code; the same answer as every
        # refusal of a token request that carries nothing but the grant.
        if data["grant_type"] == "client_credentials":
            # Nobody's sign-in is involved: the launcher's own client was
            # refused, which means Epic changed the flow under us.
            raise MalformedResponseError(f"epic refused the launcher's app token with {status}")
        if data["grant_type"] == "authorization_code":
            raise InvalidCredentialsError(
                "epic did not accept that code; it lasts a few minutes and works once, "
                "so sign in again for a fresh one"
            )
        raise InvalidCredentialsError(
            "epic has ended this sign-in; connect the account again to sign back in"
        )
    if status == 429:
        raise RateLimitedError("epic is rate limiting sign-ins", retry_after_seconds(response))
    if status >= 500:
        raise ProviderUnavailableError(f"epic answered {status} for a sign-in")
    if status != httpx.codes.OK:
        raise MalformedResponseError(f"epic answered an undocumented {status} for a sign-in")
    return _object(response, "epic's token response")


async def _get(
    client: httpx.AsyncClient,
    url: str,
    params: list[tuple[str, str]],
    token: str,
    *,
    what: str,
    missing_ok: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = await _retrying()(
        _get_once, client, url, params, token, what, missing_ok
    )
    return payload


async def _get_once(
    client: httpx.AsyncClient,
    url: str,
    params: list[tuple[str, str]],
    token: str,
    what: str,
    missing_ok: bool,
) -> dict[str, Any]:
    try:
        response = await client.get(
            url, params=tuple(params), headers={"Authorization": f"bearer {token}"}
        )
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(
            f"epic did not answer for {what}: {type(exc).__name__}"
        ) from None
    status = response.status_code
    if status == 401:
        # A token minted a moment ago: the sign-in, not the token, is the problem.
        raise InvalidCredentialsError(f"epic refused a fresh sign-in for {what}")
    if status == 404 and missing_ok:
        return {}
    if status == 429:
        raise RateLimitedError(f"epic is rate limiting {what}", retry_after_seconds(response))
    if status >= 500:
        raise ProviderUnavailableError(f"epic answered {status} for {what}")
    if status != httpx.codes.OK:
        raise MalformedResponseError(f"epic answered an undocumented {status} for {what}")
    return _object(response, what)


def _object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise MalformedResponseError(f"{what} is not JSON") from None
    if not isinstance(payload, dict):
        raise MalformedResponseError(f"{what} is not a JSON object")
    return payload


class EpicCatalog:
    """The store catalogue, asked with the launcher's app token rather than anyone's sign-in.

    Measured (#74): `client_credentials` with the launcher's client buys a token
    the catalogue answers `offers` for, so matching Epic works to IGDB needs no
    user's refresh token — and never spends one.
    """

    key: str = "epic"

    def __init__(
        self, client: httpx.AsyncClient, *, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = client
        self._monotonic = monotonic
        self._token: tuple[str, float] | None = None
        self._minting = asyncio.Lock()

    async def offers(self, namespace: str) -> list[dict[str, Any]]:
        """Every offer in the namespace, active or sunset. Short is an error, not an answer."""

        if not NAMESPACE.fullmatch(namespace):
            raise ValueError(f"not an Epic namespace: {namespace!r}")
        token = await self._app_token()
        offers: list[dict[str, Any]] = []
        while True:
            page = await _get(
                self._client,
                f"{CATALOG}/namespace/{namespace}/offers",
                [
                    ("status", "SUNSET|ACTIVE"),
                    ("country", "US"),
                    ("locale", "en"),
                    ("start", str(len(offers))),
                    ("count", str(OFFERS_PAGE)),
                ],
                token,
                what="the catalogue's offers",
                missing_ok=True,
            )
            if not page:
                return offers
            elements, paging = page.get("elements"), page.get("paging")
            total = paging.get("total") if isinstance(paging, dict) else None
            if (
                not isinstance(elements, list)
                or not all(
                    isinstance(offer, dict) and isinstance(offer.get("id"), str)
                    for offer in elements
                )
                or not isinstance(total, int)
            ):
                raise MalformedResponseError("epic returned offers without ids or a total")
            offers += elements
            if len(offers) >= total or not elements:
                if len(offers) < total:
                    raise MalformedResponseError(
                        f"epic listed {len(offers)} of {total} offers in {namespace}"
                    )
                return offers

    async def _app_token(self) -> str:
        async with self._minting:
            if self._token is not None and self._monotonic() < self._token[1]:
                return self._token[0]
            payload = await _token(self._client, {"grant_type": "client_credentials"})
            access, lifetime = payload.get("access_token"), payload.get("expires_in")
            if not isinstance(access, str) or not access or not isinstance(lifetime, int):
                raise MalformedResponseError("epic's app token response is incomplete")
            self._token = (access, self._monotonic() + lifetime - TOKEN_MARGIN_SECONDS)
            return access
