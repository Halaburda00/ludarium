"""Steam's store catalogue, asked what kind of thing an app is.

`GetOwnedGames` carries nothing that says so (#41). `store/api/appdetails`, the
endpoint usually reached for, answers `game` for all six playtests and test
clients in a real library. `IStoreBrowseService/GetItems` answers a type of its
own for a playtest, and takes a list of apps where `appdetails` takes one.

Public and unkeyed, but undocumented: every shape below was measured against the
live endpoint rather than read anywhere (ADR-0020).
"""

import json
from collections.abc import Sequence
from typing import Any, Final

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ludarium.providers.base import (
    MalformedResponseError,
    ProviderUnavailableError,
    RateLimitedError,
    retry_after_seconds,
    whole_number,
)

STORE_API: Final = "https://api.steampowered.com"
GET_ITEMS: Final = "/IStoreBrowseService/GetItems/v1/"

# The endpoint refuses POST with 405, so the ids travel in the query string, and
# a URL past roughly 8 KB is refused with 400. Measured with seven-digit appids,
# which is what Steam hands out now: 250 made 8 186 bytes and passed, 300 made
# 9 786 and failed. 200 leaves room for the eighth digit.
MAX_BATCH: Final = 200

# Without a country the store answers `200 {"response": {}}` whatever it was
# asked, which would read as "none of these exist". The language only names
# things, and nothing here reads a name.
CONTEXT: Final = {"language": "english", "country_code": "US"}

# `success` for an item the store could answer about. A delisted app comes back
# as 15 with `appid` 0, and only `id` still names what was asked.
FOUND: Final = 1

# Read at call time so a test can flatten the backoff without waiting for it.
RETRY_ATTEMPTS: Final = 3
RETRY_BACKOFF_SECONDS: Final = 0.5
RETRY_MAX_WAIT_SECONDS: Final = 8.0


class SteamStoreClient:
    """The store half of Steam, which needs no account and no key."""

    key: str = "steam_store"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def items(self, appids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """The store's record for every app it knows among `appids`, keyed as asked.

        An app the store has nothing under is left out, which the enrichment
        cache records as absent. That makes a short answer dangerous rather than
        small, so an answer that does not account for every app asked about is
        refused instead of returned.
        """

        wanted = list(dict.fromkeys(appids))
        if not wanted:
            return {}
        if len(wanted) > MAX_BATCH:
            raise ValueError(f"at most {MAX_BATCH} apps per request, not {len(wanted)}")
        malformed = [appid for appid in wanted if not appid.isdigit()]
        if malformed:
            raise ValueError(f"steam appids are digits: {malformed[:5]}")

        request = {"ids": [{"appid": int(appid)} for appid in wanted], "context": CONTEXT}
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(ProviderUnavailableError),
            wait=wait_exponential(multiplier=RETRY_BACKOFF_SECONDS, max=RETRY_MAX_WAIT_SECONDS),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            reraise=True,
        )
        # Compact, because the URL is what runs out.
        payload: dict[str, Any] = await retrying(
            self._get, json.dumps(request, separators=(",", ":"))
        )
        return _found(payload, wanted)

    async def _get(self, input_json: str) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{STORE_API}{GET_ITEMS}", params={"input_json": input_json}
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"the steam store did not answer: {type(exc).__name__}"
            ) from None

        status = response.status_code
        if status == 429:
            # Not retried: no limit is documented, so any wait would be a guess.
            raise RateLimitedError(
                "the steam store is rate limiting", retry_after=retry_after_seconds(response)
            )
        if status >= 500:
            raise ProviderUnavailableError(f"the steam store answered {status}")
        if status != httpx.codes.OK:
            raise MalformedResponseError(f"the steam store answered an undocumented {status}")
        try:
            payload = response.json()
        except ValueError:
            raise MalformedResponseError(
                "the steam store returned a body that is not JSON"
            ) from None
        if not isinstance(payload, dict):
            raise MalformedResponseError(
                f"the steam store returned a JSON {type(payload).__name__}"
            )
        return payload


def _found(payload: dict[str, Any], wanted: list[str]) -> dict[str, dict[str, Any]]:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise MalformedResponseError("the steam store returned no `response` object")
    items = response.get("store_items")
    if not isinstance(items, list):
        # What a request without a country gets: a 200 that names nothing.
        # Returned as an empty answer it would cache every app as unknown.
        raise MalformedResponseError("the steam store returned no `store_items` list")

    asked = set(wanted)
    answered: set[str] = set()
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        requested = whole_number(item.get("id")) if isinstance(item, dict) else None
        if requested is None or str(requested) not in asked:
            raise MalformedResponseError("the steam store answered about an app it was not asked")
        answered.add(str(requested))
        if item.get("success") == FOUND:
            found[str(requested)] = item
    # Measured, the store answers every id it is given, found or not.
    unanswered = asked - answered
    if unanswered:
        raise MalformedResponseError(
            f"the steam store left {len(unanswered)} of {len(asked)} apps unanswered"
        )
    return found
