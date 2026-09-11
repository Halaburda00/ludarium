import asyncio
import json
import traceback
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from ludarium.providers import (
    AppToken,
    IgdbClient,
    IgdbCredentials,
    InvalidCredentialsError,
    MalformedResponseError,
    MemoryTokenStore,
    ProviderUnavailableError,
    QueryRejectedError,
    RateLimitedError,
    RequestLimiter,
)
from ludarium.providers import igdb as igdb_module

# Documented examples rather than recordings: `popularity_types.json` is the
# response IGDB's own documentation shows for that endpoint, and `token.json` is
# the shape Twitch documents, `expires_in` included. Replace them with recorded
# responses once there is a Twitch application to record against.
FIXTURES = Path(__file__).parent / "fixtures" / "igdb"
ENDPOINT = "popularity_types"
QUERY_URL = f"{igdb_module.IGDB_API}/{ENDPOINT}"
TOKEN_URL = igdb_module.TWITCH_TOKEN_URL
CREDENTIALS = IgdbCredentials(client_id="not-a-real-client-id", client_secret="not-a-real-secret")
BODY = "fields name,popularity_source; limit 8;"
START = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
# Taken at import, before `instant_backoff` flattens it for every test.
CONFIGURED_BACKOFF = igdb_module.RETRY_BACKOFF_SECONDS


def documented(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class Clock:
    def __init__(self) -> None:
        self.moment = START

    def __call__(self) -> datetime:
        return self.moment


def generous() -> RequestLimiter:
    """A limiter that stays out of the way; it has tests of its own."""

    return RequestLimiter(per_second=1000, open_at_once=1000)


class YieldingTokenStore(MemoryTokenStore):
    """Suspends on every read and write, as a store over a database does.

    Without the suspension every query in a `gather` runs to completion before
    the next one starts, so no amount of concurrency ever reaches the mint.
    """

    async def load(self, client_id: str) -> AppToken | None:
        await asyncio.sleep(0)
        return await super().load(client_id)

    async def save(self, client_id: str, token: AppToken) -> None:
        await asyncio.sleep(0)
        await super().save(client_id, token)


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retries are the subject here, the waiting between them is not."""

    monkeypatch.setattr(igdb_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(igdb_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tokens() -> MemoryTokenStore:
    return MemoryTokenStore()


@pytest.fixture
async def igdb(clock: Clock, tokens: MemoryTokenStore) -> AsyncIterator[IgdbClient]:
    async with httpx.AsyncClient() as client:
        yield IgdbClient(CREDENTIALS, client, tokens=tokens, limiter=generous(), now=clock)


def token_route(**kwargs: Any) -> respx.Route:
    return respx.post(TOKEN_URL).mock(
        **(kwargs or {"return_value": httpx.Response(200, json=documented("token.json"))})
    )


def query_route(**kwargs: Any) -> respx.Route:
    return respx.post(QUERY_URL).mock(
        **(
            kwargs
            or {"return_value": httpx.Response(200, json=documented("popularity_types.json"))}
        )
    )


@respx.mock
async def test_a_query_is_posted_with_the_client_id_and_the_bearer_token(igdb: IgdbClient) -> None:
    token_route()
    route = query_route()

    rows = await igdb.query(ENDPOINT, BODY)

    request = route.calls.last.request
    assert request.content == BODY.encode()
    assert request.headers["Client-ID"] == CREDENTIALS.client_id
    assert request.headers["Authorization"] == "Bearer not-a-real-app-access-token"
    assert rows == documented("popularity_types.json")


@respx.mock
async def test_the_secret_travels_in_a_form_body_and_never_in_the_url(igdb: IgdbClient) -> None:
    """Twitch documents a form body; IGDB's page shows a query string. URLs get logged."""

    minted = token_route()
    query_route()

    await igdb.query(ENDPOINT, BODY)

    request = minted.calls.last.request
    assert CREDENTIALS.client_secret not in str(request.url)
    assert request.url.query == b""
    assert parse_qs(request.content.decode()) == {
        "client_id": [CREDENTIALS.client_id],
        "client_secret": [CREDENTIALS.client_secret],
        "grant_type": ["client_credentials"],
    }


@respx.mock
async def test_one_token_serves_every_query_until_it_nears_expiry(
    igdb: IgdbClient, clock: Clock
) -> None:
    minted = token_route()
    query_route()

    await igdb.query(ENDPOINT, BODY)
    await igdb.query(ENDPOINT, BODY)
    assert minted.call_count == 1

    # The documented lifetime, less the margin, plus a second.
    clock.moment = (
        START + timedelta(seconds=5011271) - igdb_module.REFRESH_MARGIN + timedelta(seconds=1)
    )
    await igdb.query(ENDPOINT, BODY)
    assert minted.call_count == 2


@respx.mock
async def test_a_stored_token_is_used_without_asking_twitch(
    igdb: IgdbClient, tokens: MemoryTokenStore
) -> None:
    """What a store that outlives the process is for."""

    await tokens.save(
        CREDENTIALS.client_id, AppToken("kept-from-last-start", START + timedelta(days=30))
    )
    minted = token_route()
    route = query_route()

    await igdb.query(ENDPOINT, BODY)

    assert minted.call_count == 0
    assert route.calls.last.request.headers["Authorization"] == "Bearer kept-from-last-start"


@respx.mock
async def test_a_token_minted_for_another_client_id_is_not_presented(
    igdb: IgdbClient, tokens: MemoryTokenStore
) -> None:
    """Changing `LUDARIUM_IGDB_CLIENT_ID` must read as a miss, not a borrowed token."""

    await tokens.save("the-previous-application", AppToken("theirs", START + timedelta(days=30)))
    minted = token_route()
    route = query_route()

    await igdb.query(ENDPOINT, BODY)

    assert minted.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer not-a-real-app-access-token"


@respx.mock
async def test_a_401_replaces_the_token_and_asks_again_once(
    igdb: IgdbClient, tokens: MemoryTokenStore
) -> None:
    await tokens.save(CREDENTIALS.client_id, AppToken("revoked", START + timedelta(days=30)))
    minted = token_route()
    route = query_route(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=documented("popularity_types.json")),
        ]
    )

    rows = await igdb.query(ENDPOINT, BODY)

    assert minted.call_count == 1
    assert [call.request.headers["Authorization"] for call in route.calls] == [
        "Bearer revoked",
        "Bearer not-a-real-app-access-token",
    ]
    assert rows == documented("popularity_types.json")
    assert await tokens.load(CREDENTIALS.client_id) != AppToken(
        "revoked", START + timedelta(days=30)
    )


@respx.mock
async def test_a_401_on_a_token_minted_this_moment_is_the_credentials(igdb: IgdbClient) -> None:
    minted = token_route()
    route = query_route(return_value=httpx.Response(401))

    with pytest.raises(InvalidCredentialsError, match="freshly issued token"):
        await igdb.query(ENDPOINT, BODY)

    assert minted.call_count == 2
    assert route.call_count == 2


@respx.mock
async def test_queries_that_all_find_no_token_share_one_mint(clock: Clock) -> None:
    minted = token_route()
    query_route()

    async with httpx.AsyncClient() as client:
        igdb = IgdbClient(
            CREDENTIALS, client, tokens=YieldingTokenStore(), limiter=generous(), now=clock
        )
        await asyncio.gather(*(igdb.query(ENDPOINT, BODY) for _ in range(5)))

    assert minted.call_count == 1


@respx.mock
async def test_queries_that_all_meet_one_revoked_token_replace_it_once(clock: Clock) -> None:
    """The one who arrives second must take the replacement, not mint another over it."""

    tokens = YieldingTokenStore()
    await tokens.save(CREDENTIALS.client_id, AppToken("revoked", START + timedelta(days=30)))
    minted = token_route()

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer revoked":
            return httpx.Response(401)
        return httpx.Response(200, json=documented("popularity_types.json"))

    query_route(side_effect=answer)

    async with httpx.AsyncClient() as client:
        igdb = IgdbClient(CREDENTIALS, client, tokens=tokens, limiter=generous(), now=clock)
        await asyncio.gather(*(igdb.query(ENDPOINT, BODY) for _ in range(4)))

    assert minted.call_count == 1


@pytest.mark.parametrize("status", [400, 401, 403])
@respx.mock
async def test_refused_client_credentials_are_reported_as_credentials(
    igdb: IgdbClient, status: int
) -> None:
    token_route(
        return_value=httpx.Response(status, json={"status": status, "message": "invalid client"})
    )
    route = query_route()

    with pytest.raises(InvalidCredentialsError, match=str(status)) as caught:
        await igdb.query(ENDPOINT, BODY)

    assert route.call_count == 0
    assert CREDENTIALS.client_secret not in str(caught.value)


@respx.mock
async def test_a_403_from_igdb_is_about_the_application_and_is_not_retried(
    igdb: IgdbClient,
) -> None:
    """Not a stale token — that is 401, and it is replaced. A refusal no new token fixes."""

    minted = token_route()
    route = query_route(return_value=httpx.Response(403))

    with pytest.raises(InvalidCredentialsError, match="403"):
        await igdb.query(ENDPOINT, BODY)

    assert route.call_count == 1
    assert minted.call_count == 1


@respx.mock
async def test_an_outage_is_retried_and_then_reported_as_one(igdb: IgdbClient) -> None:
    token_route()
    route = query_route(return_value=httpx.Response(503))

    with pytest.raises(ProviderUnavailableError, match="503"):
        await igdb.query(ENDPOINT, BODY)

    assert route.call_count == igdb_module.RETRY_ATTEMPTS


@respx.mock
async def test_a_transport_failure_is_an_outage(igdb: IgdbClient) -> None:
    token_route()
    route = query_route(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ProviderUnavailableError, match="ConnectError"):
        await igdb.query(ENDPOINT, BODY)

    assert route.call_count == igdb_module.RETRY_ATTEMPTS


@respx.mock
async def test_a_429_is_waited_out_rather_than_given_up_on(igdb: IgdbClient) -> None:
    """IGDB documents its limit per second, so a short wait is exactly the remedy."""

    token_route()
    route = query_route(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=documented("popularity_types.json")),
        ]
    )

    assert await igdb.query(ENDPOINT, BODY) == documented("popularity_types.json")
    assert route.call_count == 2


@respx.mock
async def test_a_429_that_outlasts_the_retries_is_reported_as_a_rate_limit(
    igdb: IgdbClient,
) -> None:
    token_route()
    query_route(return_value=httpx.Response(429, headers={"Retry-After": "2"}))

    with pytest.raises(RateLimitedError) as caught:
        await igdb.query(ENDPOINT, BODY)

    assert caught.value.retry_after == 2.0


def test_the_first_retry_waits_out_a_whole_rate_limit_window() -> None:
    """Less than a second after a 429 is a retry into the same window, and another 429."""

    assert CONFIGURED_BACKOFF >= 1.0


@respx.mock
async def test_a_query_igdb_will_not_run_is_not_retried(igdb: IgdbClient) -> None:
    token_route()
    route = query_route(return_value=httpx.Response(400, json=[{"title": "Syntax Error"}]))

    with pytest.raises(QueryRejectedError, match="400"):
        await igdb.query(ENDPOINT, BODY)

    assert route.call_count == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"id": 1}),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(418),
    ],
)
@respx.mock
async def test_an_answer_that_is_not_a_list_of_rows_is_malformed(
    igdb: IgdbClient, response: httpx.Response
) -> None:
    token_route()
    route = query_route(return_value=response)

    with pytest.raises(MalformedResponseError):
        await igdb.query(ENDPOINT, BODY)

    assert route.call_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "t", "token_type": "bearer"},
        {"access_token": "t", "expires_in": 0, "token_type": "bearer"},
        {"access_token": "t", "expires_in": True, "token_type": "bearer"},
        {"access_token": "t", "expires_in": "5011271", "token_type": "bearer"},
        {"access_token": "", "expires_in": 5011271, "token_type": "bearer"},
        ["not", "an", "object"],
    ],
)
@respx.mock
async def test_a_token_response_without_a_usable_token_and_lifetime_is_malformed(
    igdb: IgdbClient, payload: Any
) -> None:
    token_route(return_value=httpx.Response(200, json=payload))
    route = query_route()

    with pytest.raises(MalformedResponseError, match="twitch"):
        await igdb.query(ENDPOINT, BODY)

    assert route.call_count == 0


@pytest.mark.parametrize(
    "response", [httpx.Response(429), httpx.Response(502), httpx.Response(302)]
)
@respx.mock
async def test_twitch_failures_that_are_not_about_the_credentials_say_so(
    igdb: IgdbClient, response: httpx.Response
) -> None:
    token_route(return_value=response)
    query_route()

    with pytest.raises(
        (RateLimitedError, ProviderUnavailableError, MalformedResponseError)
    ) as caught:
        await igdb.query(ENDPOINT, BODY)

    assert not isinstance(caught.value, InvalidCredentialsError)


@respx.mock
async def test_twitch_not_answering_is_an_outage(igdb: IgdbClient) -> None:
    token_route(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ProviderUnavailableError, match="token request"):
        await igdb.query(ENDPOINT, BODY)


@pytest.mark.parametrize(
    "endpoint", ["../games", "games?limit=500", "Games", "", "games/", "_games"]
)
@respx.mock
async def test_an_endpoint_name_cannot_point_anywhere_else(igdb: IgdbClient, endpoint: str) -> None:
    minted = token_route()

    with pytest.raises(ValueError, match="endpoint"):
        await igdb.query(endpoint, BODY)

    assert minted.call_count == 0


@respx.mock
async def test_no_failure_carries_the_secret_or_the_token(
    igdb: IgdbClient, tokens: MemoryTokenStore
) -> None:
    """Checked across the whole rendered traceback, since chained exceptions print too."""

    minted = token_route(
        side_effect=httpx.ConnectError(f"refused, and here is {CREDENTIALS.client_secret}")
    )
    with pytest.raises(ProviderUnavailableError) as outage:
        await igdb.query(ENDPOINT, BODY)

    minted.mock(return_value=httpx.Response(200, json=documented("token.json")))
    query_route(return_value=httpx.Response(401))
    with pytest.raises(InvalidCredentialsError) as refused:
        await igdb.query(ENDPOINT, BODY)

    for caught in (outage, refused):
        rendered = "".join(traceback.format_exception(caught.value))
        assert CREDENTIALS.client_secret not in rendered
        assert "not-a-real-app-access-token" not in rendered


def test_neither_the_secret_nor_the_token_is_in_a_repr() -> None:
    token = AppToken("not-a-real-app-access-token", START)

    assert CREDENTIALS.client_secret not in repr(CREDENTIALS)
    assert CREDENTIALS.client_id in repr(CREDENTIALS)
    assert "not-a-real-app-access-token" not in repr(token)


class FakeTime:
    """A monotonic clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.now += seconds


async def test_the_fifth_start_in_a_second_waits_for_the_first_to_leave_the_window() -> None:
    time = FakeTime()
    limiter = RequestLimiter(
        per_second=4, open_at_once=8, monotonic=time.monotonic, sleep=time.sleep
    )

    for _ in range(4):
        async with limiter:
            pass
    assert time.slept == []

    async with limiter:
        pass
    assert time.slept == [1.0]


async def test_starts_spread_across_a_second_do_not_wait_at_all() -> None:
    time = FakeTime()
    limiter = RequestLimiter(
        per_second=4, open_at_once=8, monotonic=time.monotonic, sleep=time.sleep
    )

    for _ in range(12):
        async with limiter:
            pass
        time.now += 0.25

    assert time.slept == []


async def test_no_more_than_the_open_limit_are_in_flight_at_once() -> None:
    limiter = RequestLimiter(per_second=1000, open_at_once=8)
    inside = 0
    most = 0
    release = asyncio.Event()

    async def request() -> None:
        nonlocal inside, most
        async with limiter:
            inside += 1
            most = max(most, inside)
            await release.wait()
            inside -= 1

    tasks = [asyncio.create_task(request()) for _ in range(12)]
    await asyncio.sleep(0.01)
    assert inside == 8
    release.set()
    await asyncio.gather(*tasks)

    assert most == 8


async def test_a_wait_that_is_cancelled_gives_back_the_slot_it_held() -> None:
    """Otherwise every cancelled request shrinks the pool until nothing can start."""

    time = FakeTime()
    cancelling = True

    async def sleep(seconds: float) -> None:
        if cancelling:
            raise asyncio.CancelledError
        await time.sleep(seconds)

    limiter = RequestLimiter(per_second=1, open_at_once=1, monotonic=time.monotonic, sleep=sleep)
    async with limiter:
        pass
    with pytest.raises(asyncio.CancelledError):
        async with limiter:
            pass  # pragma: no cover

    # The same limiter, its clock now allowed to move. Had the cancelled wait
    # kept the only slot, this would block on it and the timeout would say so.
    cancelling = False
    await asyncio.wait_for(limiter.__aenter__(), timeout=1)
    await limiter.__aexit__()


@respx.mock
async def test_every_query_passes_the_limiter_and_the_token_request_does_not(
    clock: Clock, tokens: MemoryTokenStore
) -> None:
    """The limits are IGDB's, and Twitch is not IGDB."""

    entered = 0

    class Counting(RequestLimiter):
        async def __aenter__(self) -> None:
            nonlocal entered
            entered += 1
            await super().__aenter__()

    token_route()
    query_route()
    async with httpx.AsyncClient() as client:
        igdb = IgdbClient(CREDENTIALS, client, tokens=tokens, limiter=Counting(), now=clock)
        await igdb.query(ENDPOINT, BODY)
        await igdb.query(ENDPOINT, BODY)

    assert entered == 2


@respx.mock
async def test_a_429_from_twitch_is_not_retried(igdb: IgdbClient) -> None:
    """Twitch documents no limit for the token request, so any wait would be a guess.

    IGDB's 429 is retried because its window is documented as a second. This one
    is not, for the reason `SteamProvider` retries no 429 at all.
    """

    minted = token_route(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    route = query_route()

    with pytest.raises(RateLimitedError) as caught:
        await igdb.query(ENDPOINT, BODY)

    assert minted.call_count == 1
    assert route.call_count == 0
    assert not isinstance(caught.value, igdb_module.WindowExceededError)
    assert caught.value.retry_after == 30.0


class CountingTokenStore(MemoryTokenStore):
    def __init__(self) -> None:
        super().__init__()
        self.loads = 0

    async def load(self, client_id: str) -> AppToken | None:
        self.loads += 1
        return await super().load(client_id)


@respx.mock
async def test_the_store_is_not_asked_while_the_token_in_hand_is_usable(clock: Clock) -> None:
    """At four requests a second, a read per request is four database reads a second for nothing."""

    tokens = CountingTokenStore()
    token_route()
    query_route()

    async with httpx.AsyncClient() as client:
        igdb = IgdbClient(CREDENTIALS, client, tokens=tokens, limiter=generous(), now=clock)
        for _ in range(20):
            await igdb.query(ENDPOINT, BODY)

    assert tokens.loads == 1


@respx.mock
async def test_a_lapsed_token_is_looked_for_in_the_store_before_it_is_minted_again(
    clock: Clock,
) -> None:
    """Another client for the same application may already have replaced it."""

    def token(access_token: str) -> httpx.Response:
        body = {"access_token": access_token, "expires_in": 5011271, "token_type": "bearer"}
        return httpx.Response(200, json=body)

    tokens = MemoryTokenStore()
    minted = token_route(side_effect=[token("first"), token("second")])
    route = query_route()

    async with httpx.AsyncClient() as client:
        early = IgdbClient(CREDENTIALS, client, tokens=tokens, limiter=generous(), now=clock)
        await early.query(ENDPOINT, BODY)

        clock.moment = START + timedelta(seconds=5011271)
        late = IgdbClient(CREDENTIALS, client, tokens=tokens, limiter=generous(), now=clock)
        await late.query(ENDPOINT, BODY)

        await early.query(ENDPOINT, BODY)

    assert minted.call_count == 2
    assert route.calls.last.request.headers["Authorization"] == "Bearer second"


async def test_clients_for_one_application_queue_on_one_limiter() -> None:
    """IGDB's budget belongs to the application; two clients holding four each would send eight."""

    assert igdb_module.shared_limiter("one-application") is igdb_module.shared_limiter(
        "one-application"
    )
    assert igdb_module.shared_limiter("one-application") is not igdb_module.shared_limiter(
        "another-application"
    )


async def test_each_event_loop_gets_its_own_application_limiter() -> None:
    """The lock and the semaphore inside belong to the loop that first used them."""

    here = igdb_module.shared_limiter("one-application")

    def in_another_loop() -> RequestLimiter:
        async def fetch() -> RequestLimiter:
            return igdb_module.shared_limiter("one-application")

        return asyncio.run(fetch())

    there = await asyncio.to_thread(in_another_loop)

    assert there is not here


@respx.mock
async def test_a_client_without_a_limiter_of_its_own_uses_its_applications(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, tokens: MemoryTokenStore
) -> None:
    entered = 0

    class Counting(RequestLimiter):
        async def __aenter__(self) -> None:
            nonlocal entered
            entered += 1
            await super().__aenter__()

    application = Counting()
    asked: list[str] = []

    def application_limiter(client_id: str) -> RequestLimiter:
        asked.append(client_id)
        return application

    monkeypatch.setattr(igdb_module, "shared_limiter", application_limiter)
    token_route()
    query_route()

    async with httpx.AsyncClient() as client:
        for _ in range(2):
            igdb = IgdbClient(CREDENTIALS, client, tokens=tokens, now=clock)
            await igdb.query(ENDPOINT, BODY)

    assert asked == [CREDENTIALS.client_id, CREDENTIALS.client_id]
    assert entered == 2
