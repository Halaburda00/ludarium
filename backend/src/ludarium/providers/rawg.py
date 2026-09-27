"""RAWG, which is where Metacritic scores come from. Never Metacritic itself.

Runtime only: RAWG is free "as long as you attribute RAWG as the source of the
data and add an active hyperlink from every page where the data of RAWG is
used", with "no data redistribution". Nothing this client returns may be
committed or baked into an image, and the UI shows the credit wherever a score
is shown.

The key travels in the query string, which is RAWG's only way to take it. So
every error path names the endpoint and never the URL, keeps httpx out of
chained tracebacks with `raise ... from None`, and `main` silences httpx's own
request log (rule 7).

Every shape below is from RAWG's published OpenAPI document; no RAWG response is
recorded in this repository, because recording one would be redistribution.
"""

import re
from collections.abc import Iterable
from typing import Any, Final

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ludarium.providers.base import (
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderUnavailableError,
    RateLimitedError,
    retry_after_seconds,
    whole_number,
)

RAWG_API: Final = "https://api.rawg.io/api"

# How many search results are considered. Each one past the first that is not
# the game costs a `stores` request, and the free tier is 20 000 a month.
CANDIDATES: Final = 5
# The largest page `stores` is asked for. RAWG lists a game under a dozen
# stores at most, so one page is every store it has.
STORES_PAGE: Final = 40

# A Steam store link names the appid in its path; everything after is a slug.
STEAM_APP: Final = re.compile(r"^https?://store\.steampowered\.com/app/(\d+)(?:[/?#]|$)")

# Read at call time so a test can flatten the backoff without waiting for it.
RETRY_ATTEMPTS: Final = 3
RETRY_BACKOFF_SECONDS: Final = 1.0
RETRY_MAX_WAIT_SECONDS: Final = 8.0


class RawgClient:
    """Three questions: which games have this name, where is one sold, and what is it."""

    key: str = "rawg"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        # Injected, as every provider's is: the caller owns the connection pool.
        self._client = client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=***)"

    async def search(self, title: str) -> list[dict[str, Any]]:
        """The first few games RAWG files under `title`, additions left out.

        `search_precise` turns RAWG's fuzziness off. The result is still only a
        list of candidates: a name is not an identity, and the caller confirms
        one by where it is sold before believing it (rule 6).
        """

        payload = await self._get(
            "games",
            {
                "search": title,
                "search_precise": "true",
                "exclude_additions": "true",
                "page_size": str(CANDIDATES),
            },
        )
        return _results(payload, "games")

    async def stores(self, game_id: int) -> list[dict[str, Any]]:
        """Every store RAWG lists the game under, each with its link."""

        payload = await self._get(f"games/{game_id}/stores", {"page_size": str(STORES_PAGE)})
        return _results(payload, "stores")

    async def game(self, game_id: int) -> dict[str, Any] | None:
        """The game's full record, which is the only one carrying `metacritic_url`.

        None where RAWG no longer has it: a game removed between the search and
        this request is an answer, not an outage.
        """

        payload = await self._get(f"games/{game_id}", {}, missing_ok=True)
        if payload is None:
            return None
        if whole_number(payload.get("id")) != game_id:
            raise MalformedResponseError(f"rawg answered /games/{game_id} about another game")
        return payload

    async def _get(
        self, path: str, params: dict[str, str], *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(ProviderUnavailableError),
            wait=wait_exponential(multiplier=RETRY_BACKOFF_SECONDS, max=RETRY_MAX_WAIT_SECONDS),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            reraise=True,
        )
        payload: dict[str, Any] | None = await retrying(self._get_once, path, params, missing_ok)
        return payload

    async def _get_once(
        self, path: str, params: dict[str, str], missing_ok: bool
    ) -> dict[str, Any] | None:
        # Only the first segment names the endpoint in a message: the rest is
        # an id, and the query string holds the key.
        endpoint = f"/{path.split('/')[0]}"
        try:
            response = await self._client.get(
                f"{RAWG_API}/{path}", params={**params, "key": self._api_key}
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"rawg did not answer {endpoint}: {type(exc).__name__}"
            ) from None

        status = response.status_code
        if status in (401, 403):
            raise InvalidCredentialsError(f"rawg refused the api key with {status}")
        if status == 404 and missing_ok:
            return None
        if status == 429:
            # Not retried. RAWG documents a monthly allowance, not a rate, so a
            # 429 means the month is spent and no backoff would outlast it.
            raise RateLimitedError(
                f"rawg is rate limiting {endpoint}", retry_after=retry_after_seconds(response)
            )
        if status >= 500:
            raise ProviderUnavailableError(f"rawg answered {status} for {endpoint}")
        if status != httpx.codes.OK:
            raise MalformedResponseError(f"rawg answered an undocumented {status} for {endpoint}")
        try:
            payload = response.json()
        except ValueError:
            raise MalformedResponseError(
                f"rawg returned a body that is not JSON for {endpoint}"
            ) from None
        if not isinstance(payload, dict):
            raise MalformedResponseError(
                f"rawg returned a JSON {type(payload).__name__} for {endpoint}"
            )
        return payload


def steam_appids(stores: Iterable[dict[str, Any]]) -> set[str]:
    """The Steam appids a game's store links name. The hard id a candidate is confirmed by."""

    appids: set[str] = set()
    for store in stores:
        url = store.get("url")
        if isinstance(url, str) and (found := STEAM_APP.match(url.strip())):
            appids.add(found[1])
    return appids


def _results(payload: dict[str, Any] | None, what: str) -> list[dict[str, Any]]:
    results = payload.get("results") if payload is not None else None
    if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
        raise MalformedResponseError(f"rawg returned no list of {what}")
    return results
