"""IGDB, reached through the Twitch app access token it authenticates with.

Runtime only: IGDB is "free for non-commercial usage under the terms of the
Twitch Developer Service Agreement", and nothing this client returns may be
committed or baked into an image. Where fetched content is cached is #50's
decision; this module ends at a parsed response.

The client secret travels in a form body and the token in a header, so every
error path keeps both out of messages and out of chained tracebacks — `raise
... from None` wherever httpx is involved (rule 7).
"""

import asyncio
import re
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, TypeGuard

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ludarium.providers.base import (
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
    retry_after_seconds,
    whole_number,
)

IGDB_API: Final = "https://api.igdb.com/v4"
TWITCH_TOKEN_URL: Final = "https://id.twitch.tv/oauth2/token"

# IGDB's documentation: "There is a rate limit of 4 requests per second" and "You
# are able to have up to 8 open requests at any moment in time."
REQUESTS_PER_SECOND: Final = 4
OPEN_REQUESTS: Final = 8
# "The maximum value you can set for limit is 500."
MAX_LIMIT: Final = 500

# Minted again this long before Twitch says the token lapses, so a batch that
# starts on an old token does not lose it halfway. Twitch's documented example
# lifetime is 5 011 271 seconds and a token issued to this project came with
# 4 857 410 — eight weeks or so either way, of which an hour costs nothing.
REFRESH_MARGIN: Final = timedelta(hours=1)

# Read at call time so a test can flatten the backoff without waiting for it.
RETRY_ATTEMPTS: Final = 3
RETRY_BACKOFF_SECONDS: Final = 1.0
RETRY_MAX_WAIT_SECONDS: Final = 8.0

# Lowercase words joined by underscores — `games`, `external_games`. Checked
# because it becomes a path segment, and a caller's typo should not become a
# request to some other URL.
ENDPOINT: Final = re.compile(r"[a-z]+(?:_[a-z]+)*")


class QueryRejectedError(ProviderError):
    """IGDB would not run the query. A bug in the query, so never retried."""


class WindowExceededError(RateLimitedError):
    """IGDB's own 429, against a limit documented per second. The only 429 retried.

    Twitch answers a token request's 429 with a plain `RateLimitedError`. No limit
    is documented for that request, so any wait before retrying would be a guess,
    and a guessed retry into a limit is how it becomes a ban — the same reason
    `SteamProvider` retries no 429 at all.
    """


@dataclass(frozen=True, slots=True, repr=False)
class IgdbCredentials:
    """A Twitch application's client id and secret, from the environment (rule 7)."""

    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        # The default would print the secret, and a dataclass ends up in tracebacks.
        return f"{type(self).__name__}(client_id={self.client_id!r}, client_secret=***)"


@dataclass(frozen=True, slots=True, repr=False)
class AppToken:
    """A Twitch app access token. It cannot be refreshed, only replaced."""

    access_token: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"{type(self).__name__}(access_token=***, expires_at={self.expires_at.isoformat()})"


class TokenStore(Protocol):
    """Where the token outlives a request, keyed by the client id it was minted for.

    Keyed rather than single-slot, so that changing `LUDARIUM_IGDB_CLIENT_ID`
    reads as a miss instead of presenting the old application's token to IGDB.
    """

    async def load(self, client_id: str) -> AppToken | None: ...

    async def save(self, client_id: str, token: AppToken) -> None: ...


class MemoryTokenStore:
    """Holds the token for the life of the process and no longer."""

    def __init__(self) -> None:
        self._tokens: dict[str, AppToken] = {}

    async def load(self, client_id: str) -> AppToken | None:
        return self._tokens.get(client_id)

    async def save(self, client_id: str, token: AppToken) -> None:
        self._tokens[client_id] = token


class RequestLimiter:
    """Both of IGDB's limits: requests started per second, and requests open at once.

    Starts are counted in a sliding window rather than on a fixed tick, so four
    requests go out at once and the fifth waits only until the first has left
    the window. It is held around the whole request, so a slow response keeps
    its open slot until it returns — the eight-open limit is about requests IGDB
    is still answering, not requests sent.

    A client uses its application's limiter from `shared_limiter` unless it is
    handed one, so two clients cannot each spend a budget of four.
    """

    def __init__(
        self,
        *,
        per_second: int = REQUESTS_PER_SECOND,
        open_at_once: int = OPEN_REQUESTS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._per_second = per_second
        self._open = asyncio.Semaphore(open_at_once)
        self._schedule = asyncio.Lock()
        self._starts: deque[float] = deque()
        self._monotonic = monotonic
        self._sleep = sleep

    async def __aenter__(self) -> None:
        await self._open.acquire()
        try:
            async with self._schedule:
                while True:
                    now = self._monotonic()
                    while self._starts and now - self._starts[0] >= 1.0:
                        self._starts.popleft()
                    if len(self._starts) < self._per_second:
                        self._starts.append(now)
                        return
                    await self._sleep(1.0 - (now - self._starts[0]))
        except BaseException:
            # A cancelled wait must not keep the slot it never used.
            self._open.release()
            raise

    async def __aexit__(self, *_: object) -> None:
        self._open.release()


_SHARED_LIMITERS: Final[
    weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, RequestLimiter]]
] = weakref.WeakKeyDictionary()


def shared_limiter(client_id: str) -> RequestLimiter:
    """The limiter every client for one application queues on.

    IGDB does not say what it counts requests against. The `Client-ID` every
    request carries is the narrowest thing it could be, so one limiter per client
    id is the reading that cannot overshoot. Per event loop as well: the lock and
    semaphore inside belong to the loop that first used them, and a second loop —
    a test, a worker thread — gets its own rather than an error. One instance is
    one process; a second process would keep its own and is not something this
    deployment runs.
    """

    limiters = _SHARED_LIMITERS.setdefault(asyncio.get_running_loop(), {})
    limiter = limiters.get(client_id)
    if limiter is None:
        limiter = limiters[client_id] = RequestLimiter()
    return limiter


class IgdbClient:
    """An authenticated, rate-limited IGDB client that returns parsed rows."""

    def __init__(
        self,
        credentials: IgdbCredentials,
        client: httpx.AsyncClient,
        *,
        tokens: TokenStore,
        limiter: RequestLimiter | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._credentials = credentials
        # Injected rather than owned, as `SteamProvider` does: the caller decides
        # the lifetime of the connection pool.
        self._client = client
        self._tokens = tokens
        # None means the application's, looked up at the first request rather
        # than here: it needs the running loop, and a client may be built
        # before there is one.
        self._limiter = limiter
        self._now = now
        # The token in hand. The store is asked only when this is missing or no
        # longer usable — at four requests a second, asking on every request
        # would be four reads and decryptions a second for an answer already held.
        self._current: AppToken | None = None
        # One mint at a time. Without it every request that found the token
        # missing would ask Twitch for its own.
        self._minting = asyncio.Lock()

    async def query(self, endpoint: str, body: str) -> list[dict[str, Any]]:
        """Run an Apicalypse query against one endpoint and return its rows.

        Retried with backoff on an outage and on IGDB's own 429, whose limit is
        documented per second, so a second's wait is exactly what resolves it.
        Nothing else is retried — Twitch's 429 included (`WindowExceededError`).
        """

        if not ENDPOINT.fullmatch(endpoint):
            raise ValueError(f"not an IGDB endpoint name: {endpoint!r}")
        retrying = AsyncRetrying(
            retry=retry_if_exception_type((ProviderUnavailableError, WindowExceededError)),
            wait=wait_exponential(multiplier=RETRY_BACKOFF_SECONDS, max=RETRY_MAX_WAIT_SECONDS),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            reraise=True,
        )
        rows: list[dict[str, Any]] = await retrying(self._query_once, endpoint, body)
        return rows

    async def _query_once(self, endpoint: str, body: str) -> list[dict[str, Any]]:
        token = await self._token()
        response = await self._post(endpoint, body, token)
        if response.status_code == 401:
            # Twitch: "If a token becomes invalid, your API requests return HTTP
            # status code 401 Unauthorized. When this happens, you'll need to get
            # a new access token". Once — a 401 on a token minted this moment is
            # the credentials, not the token.
            token = await self._token(replacing=token)
            response = await self._post(endpoint, body, token)
            if response.status_code == 401:
                raise InvalidCredentialsError("igdb rejected a freshly issued token with 401")
        _check_igdb_status(response, endpoint)
        return _rows(response, endpoint)

    async def _token(self, *, replacing: AppToken | None = None) -> AppToken:
        async with self._minting:
            # Each candidate is judged once. Judged twice, one decision could read
            # the clock on either side of the refresh margin and mint a replacement
            # for a token it had just decided to keep.
            held = self._current
            if self._usable(held, replacing):
                return held
            # Missing at the first request, lapsed later, or the one just rejected.
            # The store may already hold its replacement — minted by another client
            # for this application — and that is worth one read before a mint.
            stored = await self._tokens.load(self._credentials.client_id)
            if self._usable(stored, replacing):
                self._current = stored
                return stored
            fresh = await self._mint()
            await self._tokens.save(self._credentials.client_id, fresh)
            self._current = fresh
            return fresh

    def _usable(self, token: AppToken | None, replacing: AppToken | None) -> TypeGuard[AppToken]:
        return (
            token is not None
            and token != replacing
            and self._now() < token.expires_at - REFRESH_MARGIN
        )

    async def _post(self, endpoint: str, body: str, token: AppToken) -> httpx.Response:
        limiter = self._limiter or shared_limiter(self._credentials.client_id)
        async with limiter:
            try:
                return await self._client.post(
                    f"{IGDB_API}/{endpoint}",
                    content=body.encode(),
                    headers={
                        "Client-ID": self._credentials.client_id,
                        "Authorization": f"Bearer {token.access_token}",
                        "Accept": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(
                    f"igdb did not answer /{endpoint}: {type(exc).__name__}"
                ) from None

    async def _mint(self) -> AppToken:
        # A form body, as Twitch documents it. IGDB's own page shows a query
        # string, which would put the secret in a URL — and URLs get logged.
        try:
            response = await self._client.post(
                TWITCH_TOKEN_URL,
                data={
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"twitch did not answer the token request: {type(exc).__name__}"
            ) from None

        status = response.status_code
        # Twitch does not list this request's failure statuses. Any refusal of a
        # request that carries nothing but the credentials is about them.
        if status in (400, 401, 403):
            raise InvalidCredentialsError(
                f"twitch rejected the IGDB client credentials with {status}"
            )
        if status == 429:
            # Plain, so it is not retried: see `WindowExceededError`.
            raise RateLimitedError(
                "twitch is rate limiting token requests", retry_after=retry_after_seconds(response)
            )
        if status >= 500:
            raise ProviderUnavailableError(f"twitch answered {status} for a token")
        if status != httpx.codes.OK:
            raise MalformedResponseError(f"twitch answered an undocumented {status} for a token")

        payload = _json(response, "the twitch token response")
        if not isinstance(payload, dict):
            raise MalformedResponseError("twitch returned a token response that is not an object")
        access_token = payload.get("access_token")
        lifetime = whole_number(payload.get("expires_in"))
        if not isinstance(access_token, str) or not access_token:
            raise MalformedResponseError("twitch returned a token response without an access_token")
        if lifetime is None or lifetime <= 0:
            raise MalformedResponseError(
                "twitch returned a token response without a usable expires_in"
            )
        return AppToken(
            access_token=access_token, expires_at=self._now() + timedelta(seconds=lifetime)
        )


def _check_igdb_status(response: httpx.Response, endpoint: str) -> None:
    status = response.status_code
    if status == 403:
        raise InvalidCredentialsError(f"igdb refused /{endpoint} with 403")
    if status == 429:
        raise WindowExceededError(
            f"igdb is rate limiting /{endpoint}", retry_after=retry_after_seconds(response)
        )
    if status >= 500:
        raise ProviderUnavailableError(f"igdb answered {status} for /{endpoint}")
    if status == 400:
        raise QueryRejectedError(f"igdb rejected the query for /{endpoint} with 400")
    if status != httpx.codes.OK:
        raise MalformedResponseError(f"igdb answered an undocumented {status} for /{endpoint}")


def _rows(response: httpx.Response, endpoint: str) -> list[dict[str, Any]]:
    payload = _json(response, f"/{endpoint}")
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise MalformedResponseError(
            f"igdb returned something other than a list of objects for /{endpoint}"
        )
    return payload


def _json(response: httpx.Response, what: str) -> Any:
    try:
        return response.json()
    except ValueError:
        raise MalformedResponseError(f"{what} is not JSON") from None
