"""GOG's library, through the GOG Galaxy client's own endpoints (ADR-0037).

GOG has no public library API. What is used here is what GOG Galaxy itself
calls, as Heroic and lgogdownloader do: the user signs in on gog.com, pastes
back the code the redirect page carries, and the code is exchanged for a
refresh token that is kept, encrypted, as the account's credential. Every
endpoint and field below was measured against a real library (#128) before
anything relied on it.

The library is three answers. The account's product list names its games, in
English, with their slugs. The owned ids name everything else it holds, of which
only add-ons are library items; the public catalogue says which ids those are
and which game each belongs to. The profile's statistics give playtime and the
last session.

Unlike Epic's, the refresh token is not replaced on use (measured), so
`renewed_secret` stays None. Tokens travel in form bodies and headers and never
in a URL, and every error path keeps them out of messages with `raise ... from
None` (rule 7).
"""

import re
from collections.abc import Mapping, Sequence
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

TOKEN: Final = "https://auth.gog.com/token"
ACCOUNT_HOST: Final = "embed.gog.com"
PRODUCTS: Final = "https://embed.gog.com/account/getFilteredProducts"
OWNED: Final = "https://embed.gog.com/user/data/games"
USER: Final = "https://embed.gog.com/userData.json"
STATS: Final = "https://www.gog.com/u/{username}/games/stats"
CATALOG: Final = "https://api.gog.com/products"
CATALOG_V2: Final = "https://api.gog.com/v2/games/{id}"

# GOG Galaxy's own client, public in every Galaxy install, in Heroic and in
# lgogdownloader. It identifies the program asking; the user's sign-in is what
# grants access.
CLIENT_ID: Final = "46899977096215655"
CLIENT_SECRET: Final = (
    "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"  # gitleaks:allow
)
REDIRECT_URI: Final = "https://embed.gog.com/on_login_success?origin=client"
LOGIN_URL: Final = (
    f"https://auth.gog.com/auth?client_id={CLIENT_ID}"
    "&redirect_uri=https%3A%2F%2Fembed.gog.com%2Fon_login_success%3Forigin%3Dclient"
    "&response_type=code&layout=client2"
)

# Catalogue ids per request. The public endpoint answers fifty at once.
CATALOG_BATCH: Final = 50
# A list that has not ended after this many pages is not ending. Measured:
# 100 products and 50 statistics a page, two and four pages for 190 games.
MAX_PAGES: Final = 200
# The redirect page carries the code in its address, and the user may paste
# either.
CODE_IN_URL: Final = re.compile(r"[?&]code=([^&#\s]+)")
# A product link names its id last: `https://api.gog.com/v2/games/1519147341?…`.
GAME_LINK: Final = re.compile(r"/v2/games/(\d+)")

RETRY_ATTEMPTS: Final = 3
RETRY_BACKOFF_SECONDS: Final = 0.5
RETRY_MAX_WAIT_SECONDS: Final = 8.0


@dataclass(frozen=True, slots=True, repr=False)
class GogSignIn:
    """What a code is exchanged for: whose account, and the token to keep."""

    user_id: str
    refresh_token: str

    def __repr__(self) -> str:
        return f"{type(self).__name__}(user_id={self.user_id!r}, refresh_token=***)"


async def sign_in(client: httpx.AsyncClient, pasted: str) -> GogSignIn:
    """Exchange the code the redirect page carried. It is single-use and short-lived."""

    pasted = pasted.strip().strip('"')
    found = CODE_IN_URL.search(pasted)
    code = found.group(1) if found else pasted
    if not code:
        raise InvalidCredentialsError("paste the address GOG's page went to, or its code")
    payload = await _token(
        client, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    )
    return GogSignIn(user_id=_user(payload), refresh_token=_refresh_token(payload))


class GogProvider:
    """One connected GOG account."""

    key: str = "gog"
    # GOG keeps the refresh token across uses (ADR-0037).
    renewed_secret: str | None = None

    def __init__(self, refresh_token: str, user_id: str, client: httpx.AsyncClient) -> None:
        self._refresh_token = refresh_token
        self._user_id = user_id
        self._client = client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(user_id={self._user_id!r}, refresh_token=***)"

    async def validate_credentials(self) -> None:
        await self._access_token()

    async def fetch_library(self) -> FetchedLibrary:
        token = await self._access_token()
        games = await self._games(token)
        stats = await self._stats(token)
        owned = await self._owned(token)
        addons = await self._addons([item for item in owned if item not in games])
        items = [_as_game(game, stats.get(product_id)) for product_id, game in games.items()]
        items += [_as_addon(product_id, *addon) for product_id, addon in addons.items()]
        return FetchedLibrary(items)

    async def _access_token(self) -> str:
        payload = await _token(
            self._client, {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        )
        if _user(payload) != self._user_id:
            # The stored token is another account's: syncing it would put
            # someone else's library under this row.
            raise InvalidCredentialsError("gog answered for a different account than this one")
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise MalformedResponseError("gog's token response has no access_token")
        return access

    async def _games(self, token: str) -> dict[str, dict[str, Any]]:
        """The account's games, by product id: every page, or an error (rule 1)."""

        games: dict[str, dict[str, Any]] = {}
        pages = 1
        page = 1
        while page <= pages:
            if page > MAX_PAGES:
                raise MalformedResponseError(f"gog's products did not end after {MAX_PAGES} pages")
            answer = await _get(
                self._client,
                PRODUCTS,
                token,
                params={"mediaType": "1", "page": str(page)},
                what="the products",
            )
            found = answer.get("products")
            total = answer.get("totalPages")
            if not isinstance(found, list) or not isinstance(total, int):
                raise MalformedResponseError("gog returned a products page without its products")
            for product in found:
                if not isinstance(product, dict) or not isinstance(product.get("id"), int):
                    raise MalformedResponseError("gog returned a product without an id")
                games[str(product["id"])] = product
            pages = total
            page += 1
        return games

    async def _stats(self, token: str) -> dict[str, Mapping[str, Any]]:
        """Playtime and last session by product id, from the profile's statistics.

        Keyed by the profile's name, which is read here each time and kept
        nowhere: it is not the account's identity, and the user can rename it.
        """

        user = await _get(self._client, USER, token, what="the account")
        username = user.get("username")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
            raise MalformedResponseError("gog's account answer names no profile")
        stats: dict[str, Mapping[str, Any]] = {}
        pages = 1
        page = 1
        while page <= pages:
            if page > MAX_PAGES:
                raise MalformedResponseError(
                    f"gog's statistics did not end after {MAX_PAGES} pages"
                )
            answer = await _get(
                self._client,
                STATS.format(username=username),
                token,
                params={"page": str(page)},
                what="the statistics",
            )
            total = answer.get("pages")
            embedded = answer.get("_embedded")
            found = embedded.get("items") if isinstance(embedded, dict) else None
            if not isinstance(found, list) or not isinstance(total, int):
                raise MalformedResponseError("gog returned a statistics page without its items")
            for entry in found:
                game = entry.get("game") if isinstance(entry, dict) else None
                product_id = game.get("id") if isinstance(game, dict) else None
                # Keyed by the user id, or an empty list where there is nothing
                # to say: measured on 41 of 191 games.
                mine = entry.get("stats") if isinstance(entry, dict) else None
                figures = mine.get(self._user_id) if isinstance(mine, dict) else None
                if isinstance(product_id, str) and isinstance(figures, dict):
                    stats[product_id] = figures
            pages = total
            page += 1
        return stats

    async def _owned(self, token: str) -> list[str]:
        answer = await _get(self._client, OWNED, token, what="the owned ids")
        owned = answer.get("owned")
        if not isinstance(owned, list) or not all(isinstance(item, int) for item in owned):
            raise MalformedResponseError("gog returned owned ids that are not a list of numbers")
        return [str(item) for item in owned]

    async def _addons(self, ids: Sequence[str]) -> dict[str, tuple[str, str | None]]:
        """Each add-on among `ids`, with its title and the game it needs.

        The rest are packs, whose games are already in the product list, and ids
        the public catalogue does not know at all: 125 of 362 measured, neither
        nameable nor a library item, and left out rather than counted as lost.
        """

        addons: dict[str, str] = {}
        named = 0
        for start in range(0, len(ids), CATALOG_BATCH):
            batch = ids[start : start + CATALOG_BATCH]
            found = await _get_list(
                self._client, CATALOG, params={"ids": ",".join(batch)}, what="the catalogue"
            )
            named += len(found)
            for product in found:
                if (
                    isinstance(product, dict)
                    and product.get("game_type") == "dlc"
                    and isinstance(product.get("id"), int)
                    and isinstance(product.get("title"), str)
                ):
                    addons[str(product["id"])] = product["title"]
        # A catalogue that knows none of them is an outage in the shape of an
        # answer: measured, it named 47 of 172. Read as "no add-ons", the sweep
        # would remove every add-on the account owns (rule 1). A partial answer
        # cannot be told apart from ids it never knew, and its cost is an
        # add-on marked removed until the next run lists it again.
        if ids and not named:
            raise MalformedResponseError(
                f"gog's catalogue named none of the {len(ids)} owned ids it was asked about"
            )
        parents: dict[str, tuple[str, str | None]] = {}
        for product_id, title in addons.items():
            # Gone from the v2 catalogue while v1 still lists it, as a delisted
            # add-on may be: still owned, and only its game goes unnamed.
            detail = await _get(
                self._client,
                CATALOG_V2.format(id=product_id),
                None,
                what="an add-on",
                missing_ok=True,
            )
            parents[product_id] = (title, _required_game(detail))
        return parents


def _required_game(detail: Mapping[str, Any]) -> str | None:
    """The game an add-on needs, from the catalogue's `requiresGames` link (#98)."""

    links = detail.get("_links")
    required = links.get("requiresGames") if isinstance(links, dict) else None
    for link in required if isinstance(required, list) else []:
        href = link.get("href") if isinstance(link, dict) else None
        found = GAME_LINK.search(href) if isinstance(href, str) else None
        if found:
            return found.group(1)
    return None


def _as_game(product: Mapping[str, Any], stats: Mapping[str, Any] | None) -> LibraryItem:
    title = product.get("title")
    playtime = (stats or {}).get("playtime")
    return LibraryItem(
        provider_item_id=str(product["id"]),
        title=title if isinstance(title, str) else "",
        # Nothing in the answer tells a giveaway from a purchase.
        ownership_type=OwnershipType.OWNED,
        item_kind=ItemKind.GAME,
        # Minutes (ADR-0037).
        playtime_minutes=playtime if isinstance(playtime, int) and playtime >= 0 else None,
        last_played_at=_moment((stats or {}).get("lastSession")),
        raw={
            "id": product["id"],
            "slug": product.get("slug"),
            "category": product.get("category"),
        },
    )


def _as_addon(product_id: str, title: str, parent: str | None) -> LibraryItem:
    return LibraryItem(
        provider_item_id=product_id,
        title=title,
        ownership_type=OwnershipType.OWNED,
        item_kind=ItemKind.DLC,
        parent_item_id=parent,
        raw={"id": int(product_id), "requiresGame": parent},
    )


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _user(payload: Mapping[str, Any]) -> str:
    user = payload.get("user_id")
    if not isinstance(user, str) or not user:
        raise MalformedResponseError("gog's token response names no user")
    return user


def _refresh_token(payload: Mapping[str, Any]) -> str:
    token = payload.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise MalformedResponseError("gog's token response has no refresh_token")
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
    try:
        response = await client.post(
            TOKEN, data={**data, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        )
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(f"gog did not answer: {type(exc).__name__}") from None
    status = response.status_code
    if status in (400, 401, 403):
        # `invalid_grant`, measured for a spent code and for an unknown token.
        if data["grant_type"] == "authorization_code":
            raise InvalidCredentialsError(
                "gog did not accept that code; it lasts a few minutes and works once, "
                "so sign in again for a fresh one"
            )
        raise InvalidCredentialsError(
            "gog has ended this sign-in; connect the account again to sign back in"
        )
    if status == 429:
        raise RateLimitedError("gog is rate limiting sign-ins", retry_after_seconds(response))
    if status >= 500:
        raise ProviderUnavailableError(f"gog answered {status} for a sign-in")
    if status != httpx.codes.OK:
        raise MalformedResponseError(f"gog answered an undocumented {status} for a sign-in")
    return _object(response, "gog's token response")


async def _get(
    client: httpx.AsyncClient,
    url: str,
    token: str | None,
    *,
    params: Mapping[str, str] | None = None,
    what: str,
    missing_ok: bool = False,
) -> dict[str, Any]:
    response: httpx.Response = await _retrying()(
        _get_once, client, url, token, params, what, missing_ok
    )
    if missing_ok and response.status_code == httpx.codes.NOT_FOUND:
        return {}
    return _object(response, what)


async def _get_list(
    client: httpx.AsyncClient, url: str, *, params: Mapping[str, str], what: str
) -> list[Any]:
    response: httpx.Response = await _retrying()(_get_once, client, url, None, params, what, False)
    try:
        payload = response.json()
    except ValueError:
        raise MalformedResponseError(f"{what} is not JSON") from None
    if not isinstance(payload, list):
        raise MalformedResponseError(f"{what} is not a JSON list")
    return payload


async def _get_once(
    client: httpx.AsyncClient,
    url: str,
    token: str | None,
    params: Mapping[str, str] | None,
    what: str,
    missing_ok: bool,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(
            f"gog did not answer for {what}: {type(exc).__name__}"
        ) from None
    status = response.status_code
    if status in (301, 302, 303, 307, 401, 403) and token and response.url.host == ACCOUNT_HOST:
        # Measured on the account endpoints: a refused token is sent to the
        # sign-in page rather than answered 401. The token was minted a moment
        # ago, so the sign-in, not the token, is what failed. Not on the
        # profile's host, where it was not measured and a redirect may be about
        # the profile; there it is an answer this code cannot read.
        raise InvalidCredentialsError(f"gog refused a fresh sign-in for {what}")
    if status == httpx.codes.NOT_FOUND and missing_ok:
        return response
    if status == 429:
        raise RateLimitedError(f"gog is rate limiting {what}", retry_after_seconds(response))
    if status >= 500:
        raise ProviderUnavailableError(f"gog answered {status} for {what}")
    if status != httpx.codes.OK:
        raise MalformedResponseError(f"gog answered an undocumented {status} for {what}")
    return response


def _object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise MalformedResponseError(f"{what} is not JSON") from None
    if not isinstance(payload, dict):
        raise MalformedResponseError(f"{what} is not a JSON object")
    return payload
