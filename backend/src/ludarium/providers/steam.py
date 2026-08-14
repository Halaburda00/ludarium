"""Steam's owned-games API, normalised.

The key travels as a query parameter, so every error path here is written to
keep it out of messages and out of chained tracebacks — `raise ... from None`
rather than `from exc` wherever httpx is involved (rule 7).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ludarium.providers.base import (
    InvalidCredentialsError,
    LibraryItem,
    LibraryNotVisibleError,
    MalformedResponseError,
    ProviderUnavailableError,
    RateLimitedError,
)

STEAM_API = "https://api.steampowered.com"
OWNED_GAMES = "/IPlayerService/GetOwnedGames/v1/"

# Read at call time so a test can flatten the backoff without waiting for it.
RETRY_ATTEMPTS: Final = 3
RETRY_BACKOFF_SECONDS: Final = 0.5
RETRY_MAX_WAIT_SECONDS: Final = 8.0


@dataclass(frozen=True, slots=True, repr=False)
class SteamCredentials:
    """The user's own Web API key, plus whose library to ask about."""

    api_key: str
    steam_id: str

    def __repr__(self) -> str:
        # The default would print the key, and a dataclass ends up in tracebacks.
        return f"{type(self).__name__}(api_key=***, steam_id={self.steam_id!r})"


class SteamProvider:
    """One connected Steam account."""

    key: str = "steam"

    def __init__(self, credentials: SteamCredentials, client: httpx.AsyncClient) -> None:
        self._credentials = credentials
        # Injected rather than owned: the caller decides the lifetime of the
        # connection pool, and one client serves every account.
        self._client = client

    async def validate_credentials(self) -> None:
        await self._owned_games()

    async def fetch_library(self) -> list[LibraryItem]:
        return [_as_item(game) for game in await self._owned_games()]

    async def _owned_games(self) -> list[dict[str, Any]]:
        payload = await self._request(
            OWNED_GAMES,
            {
                "key": self._credentials.api_key,
                "steamid": self._credentials.steam_id,
                "include_appinfo": 1,
                # Free games the user has played are in the library as far as
                # anyone looking at it is concerned. Steam does not say which
                # they are, so they stay `owned` rather than becoming a guess.
                "include_played_free_games": 1,
                # English regardless of the UI language: the matcher compares
                # these titles across platforms and needs one spelling.
                "l": "english",
                "format": "json",
            },
        )
        response = payload.get("response")
        if not isinstance(response, dict):
            raise MalformedResponseError("steam returned no `response` object")
        # A private profile is an empty object; an empty public library still
        # carries `game_count`. The difference matters — one is the user's to
        # fix and the other is nothing at all.
        if not response:
            raise LibraryNotVisibleError(
                "steam returned an empty response, which means the profile's "
                "game details are private"
            )
        games = response.get("games", [])
        if not isinstance(games, list):
            raise MalformedResponseError("steam returned a `games` value that is not a list")
        _check_against_count(games, response.get("game_count"))
        return games

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        # Transient only: a rejected key or an unreadable body will be exactly
        # as wrong the second time, and a 429 answered with an immediate retry
        # is how a rate limit becomes a ban.
        retrying = AsyncRetrying(
            retry=retry_if_exception_type(ProviderUnavailableError),
            wait=wait_exponential(multiplier=RETRY_BACKOFF_SECONDS, max=RETRY_MAX_WAIT_SECONDS),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            reraise=True,
        )
        payload: dict[str, Any] = await retrying(self._get, path, params)
        return payload

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{STEAM_API}{path}", params=params)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"steam did not answer {path}: {type(exc).__name__}"
            ) from None

        _check_status(response, path)
        try:
            payload = response.json()
        except ValueError:
            raise MalformedResponseError(
                f"steam returned a body that is not JSON for {path}"
            ) from None
        if not isinstance(payload, dict):
            raise MalformedResponseError(
                f"steam returned a JSON {type(payload).__name__} for {path}"
            )
        return payload


def _check_status(response: httpx.Response, path: str) -> None:
    status = response.status_code
    if status in (401, 403):
        raise InvalidCredentialsError(f"steam rejected the API key with {status}")
    if status == 429:
        raise RateLimitedError(
            "steam is rate limiting this key", retry_after=_retry_after(response)
        )
    if status >= 500:
        raise ProviderUnavailableError(f"steam answered {status} for {path}")
    if status != httpx.codes.OK:
        raise MalformedResponseError(f"steam answered an undocumented {status} for {path}")


def _check_against_count(games: list[Any], count: object) -> None:
    """Fewer games than Steam says it has is a short answer, not a small library.

    An empty `games` is legitimate — that is what an empty public library looks
    like — so the count is the only thing that separates it from a truncated
    body. Getting this wrong is a rule 1 failure with nothing to notice it: the
    sync would finish `success` with zero items and clear the whole Steam
    library into the removed view.

    Only the short direction is refused. More entries than the count cannot
    cause a removal, and `include_played_free_games` is the kind of parameter
    that makes a platform's own tally drift from the list it sends.
    """

    promised = _whole(count)
    if promised is not None and len(games) < promised:
        raise MalformedResponseError(f"steam said it has {promised} games and sent {len(games)}")


def _retry_after(response: httpx.Response) -> float | None:
    """Steam's own figure, where it gave one. A date-form header is not worth parsing."""

    header = response.headers.get("retry-after", "")
    try:
        return float(header)
    except ValueError:
        return None


def _as_item(game: dict[str, Any]) -> LibraryItem:
    appid, name = _whole(game.get("appid")), game.get("name")
    if appid is None or not isinstance(name, str):
        raise MalformedResponseError("a steam library entry has no usable appid or name")
    return LibraryItem(
        provider_item_id=str(appid),
        title=name,
        playtime_minutes=_minutes(game.get("playtime_forever")),
        last_played_at=_moment(game.get("rtime_last_played")),
        raw=game,
    )


def _whole(value: object) -> int | None:
    """An integer Steam actually sent.

    In one place rather than at each call site: `bool` is an `int` in Python and
    is never a number here, and a rule spelled out three times is a rule that
    holds in two of them.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _minutes(value: object) -> int | None:
    # Steam counts in minutes already.
    minutes = _whole(value)
    return minutes if minutes is not None and minutes >= 0 else None


def _moment(value: object) -> datetime | None:
    seconds = _whole(value)
    # Zero is "never played", which is not the epoch.
    return datetime.fromtimestamp(seconds, UTC) if seconds is not None and seconds > 0 else None
