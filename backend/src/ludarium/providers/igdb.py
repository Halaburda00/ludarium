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
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ludarium.providers.base import (
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitedError,
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
# lifetime is 5 011 271 seconds, about 58 days; an hour of that costs nothing.
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

    Enforced here and nowhere else, so no later caller can exceed it by existing.
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
        self._limiter = limiter or RequestLimiter()
        self._now = now
        # One mint at a time. Without it every request that found the token
        # missing would ask Twitch for its own.
        self._minting = asyncio.Lock()

    async def query(self, endpoint: str, body: str) -> list[dict[str, Any]]:
        """Run an Apicalypse query against one endpoint and return its rows.

        Retried with backoff on an outage and on 429. Steam does not retry a
        429, because its limits are opaque and a quick retry is how a limit
        becomes a ban; IGDB's is documented as per second, so a second's wait is
        exactly what resolves it.
        """

        if not ENDPOINT.fullmatch(endpoint):
            raise ValueError(f"not an IGDB endpoint name: {endpoint!r}")
        retrying = AsyncRetrying(
            retry=retry_if_exception_type((ProviderUnavailableError, RateLimitedError)),
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
            stored = await self._tokens.load(self._credentials.client_id)
            # A different token than the one being replaced means another request
            # minted while this one waited for the lock, and it is the answer.
            if (
                stored is not None
                and stored != replacing
                and self._now() < stored.expires_at - REFRESH_MARGIN
            ):
                return stored
            fresh = await self._mint()
            await self._tokens.save(self._credentials.client_id, fresh)
            return fresh

    async def _post(self, endpoint: str, body: str, token: AppToken) -> httpx.Response:
        async with self._limiter:
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
            raise RateLimitedError(
                "twitch is rate limiting token requests", retry_after=_retry_after(response)
            )
        if status >= 500:
            raise ProviderUnavailableError(f"twitch answered {status} for a token")
        if status != httpx.codes.OK:
            raise MalformedResponseError(f"twitch answered an undocumented {status} for a token")

        payload = _json(response, "the twitch token response")
        if not isinstance(payload, dict):
            raise MalformedResponseError("twitch returned a token response that is not an object")
        access_token, lifetime = payload.get("access_token"), _whole(payload.get("expires_in"))
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
        raise RateLimitedError(
            f"igdb is rate limiting /{endpoint}", retry_after=_retry_after(response)
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


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after", "")
    try:
        return float(header)
    except ValueError:
        return None


def _whole(value: object) -> int | None:
    # `bool` is an `int` in Python and is never a number here.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
