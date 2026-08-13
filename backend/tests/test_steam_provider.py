import json
import socket
import traceback
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ludarium.providers import (
    InvalidCredentialsError,
    LibraryNotVisibleError,
    LibraryProvider,
    MalformedResponseError,
    ProviderUnavailableError,
    RateLimitedError,
    SteamCredentials,
    SteamProvider,
)
from ludarium.providers import steam as steam_module

FIXTURES = Path(__file__).parent / "fixtures" / "steam"
OWNED_GAMES_URL = f"{steam_module.STEAM_API}{steam_module.OWNED_GAMES}"
API_KEY = "0123456789ABCDEF-not-a-real-key"
STEAM_ID = "76561197960287930"


def recorded(name: str) -> str:
    return (FIXTURES / name).read_text()


def recorded_json(name: str) -> Any:
    return json.loads(recorded(name))


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retries are the subject here, the waiting between them is not."""

    monkeypatch.setattr(steam_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(steam_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def provider() -> AsyncIterator[SteamProvider]:
    async with httpx.AsyncClient() as client:
        yield SteamProvider(SteamCredentials(api_key=API_KEY, steam_id=STEAM_ID), client)


def test_the_suite_cannot_reach_the_network() -> None:
    """The guard behind "no test reaches the network", asserted rather than assumed.

    A literal address, so nothing resolves a name either: this has to fail
    because sockets are blocked, not because CI happens to be offline.
    """

    blocked = socket.socket()
    # So that removing the guard fails this in a tenth of a second rather than
    # hanging CI on a reserved address that never answers.
    blocked.settimeout(0.1)

    with pytest.raises(RuntimeError, match="recorded fixtures"):
        blocked.connect(("192.0.2.1", 443))


def test_the_steam_provider_is_a_library_provider(provider: SteamProvider) -> None:
    """Structural, so M4 can add GOG without either side importing the other."""

    library: LibraryProvider = provider

    assert library.key == "steam"


@respx.mock
async def test_a_library_comes_back_normalised(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded_json("owned_games.json"))
    )

    items = await provider.fetch_library()

    assert [item.provider_item_id for item in items] == ["292030", "620", "570"]
    witcher, portal, _ = items
    assert witcher.title == "The Witcher 3: Wild Hunt"
    assert witcher.playtime_minutes == 3247
    assert witcher.last_played_at == datetime(2025, 4, 18, 18, 13, 20, tzinfo=UTC)
    # Never launched: zero is not the epoch, and no playtime is not zero playtime.
    assert portal.last_played_at is None
    assert portal.playtime_minutes == 0
    # Carried through for M1 even though nothing displays it yet.
    assert witcher.raw["img_icon_url"]


@respx.mock
async def test_titles_are_requested_in_english(provider: SteamProvider) -> None:
    """Independent of the UI language: the matcher compares these across platforms."""

    route = respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded_json("owned_games.json"))
    )

    await provider.fetch_library()

    params = route.calls.last.request.url.params
    assert params["l"] == "english"
    assert params["include_appinfo"] == "1"
    assert params["steamid"] == STEAM_ID


@respx.mock
async def test_an_empty_library_is_not_a_failure(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded_json("owned_games_empty.json"))
    )

    assert await provider.fetch_library() == []


@respx.mock
async def test_a_private_profile_is_its_own_failure(provider: SteamProvider) -> None:
    """Told "invalid key" the user would rotate credentials that were never wrong."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded_json("private_profile.json"))
    )

    with pytest.raises(LibraryNotVisibleError, match="private"):
        await provider.fetch_library()


@respx.mock
async def test_a_rejected_key_is_not_retried(provider: SteamProvider) -> None:
    route = respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(401, html=recorded("unauthorised.html"))
    )

    with pytest.raises(InvalidCredentialsError):
        await provider.validate_credentials()

    assert route.call_count == 1


@respx.mock
async def test_the_key_never_appears_in_a_failure(provider: SteamProvider) -> None:
    """Rule 7, including the chained traceback — the key is a query parameter.

    httpx puts the request URL in its own exceptions, so `raise ... from exc`
    would leak the key into every log line that formats the chain.
    """

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(401, html=recorded("unauthorised.html"))
    )

    with pytest.raises(InvalidCredentialsError) as caught:
        await provider.fetch_library()

    rendered = "".join(traceback.format_exception(caught.value))
    assert API_KEY not in rendered
    assert API_KEY not in repr(SteamCredentials(api_key=API_KEY, steam_id=STEAM_ID))


@respx.mock
async def test_rate_limiting_carries_the_platform_s_own_figure(provider: SteamProvider) -> None:
    """Answered with an immediate retry, a rate limit is how a key becomes a ban."""

    route = respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "120"}, html=recorded("rate_limited.html")
        )
    )

    with pytest.raises(RateLimitedError) as caught:
        await provider.fetch_library()

    assert caught.value.retry_after == 120.0
    assert route.call_count == 1


@respx.mock
async def test_a_rate_limit_without_a_header_still_reports(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(RateLimitedError) as caught:
        await provider.fetch_library()

    assert caught.value.retry_after is None


@respx.mock
async def test_a_server_error_is_retried_and_then_surfaces(provider: SteamProvider) -> None:
    """Rule 1 depends on this type: a run that ends here removes nothing."""

    route = respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(ProviderUnavailableError):
        await provider.fetch_library()

    assert route.call_count == steam_module.RETRY_ATTEMPTS


@respx.mock
async def test_a_transport_failure_is_retried_and_may_still_succeed(
    provider: SteamProvider,
) -> None:
    route = respx.get(OWNED_GAMES_URL).mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=recorded_json("owned_games.json")),
        ]
    )

    items = await provider.fetch_library()

    assert len(items) == 3
    assert route.call_count == 2


@respx.mock
async def test_a_malformed_body_is_not_retried(provider: SteamProvider) -> None:
    """Wrong twice as fast is not worth the second request."""

    route = respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded_json("malformed.json"))
    )

    with pytest.raises(MalformedResponseError, match="not a list"):
        await provider.fetch_library()

    assert route.call_count == 1


@respx.mock
async def test_a_body_that_is_not_json_is_a_malformed_response(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, html=recorded("unauthorised.html"))
    )

    with pytest.raises(MalformedResponseError, match="not JSON"):
        await provider.fetch_library()


@respx.mock
async def test_a_body_without_a_response_object_is_malformed(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(200, json={"games": []}))

    with pytest.raises(MalformedResponseError, match="`response`"):
        await provider.fetch_library()


@respx.mock
async def test_a_json_body_that_is_not_an_object_is_malformed(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(200, json=[1, 2, 3]))

    with pytest.raises(MalformedResponseError, match="JSON list"):
        await provider.fetch_library()


@respx.mock
async def test_an_undocumented_status_is_malformed(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(302))

    with pytest.raises(MalformedResponseError, match="undocumented"):
        await provider.fetch_library()


@respx.mock
async def test_a_playtime_that_is_not_a_duration_is_dropped(provider: SteamProvider) -> None:
    """One nonsensical field is not worth failing a whole library over.

    An appid or a name we cannot read makes the entry unusable; a playtime we
    cannot read makes one column null, and `max` in the resolver treats that as
    "this source has no figure" rather than as zero.
    """

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "response": {
                    "game_count": 1,
                    "games": [
                        {
                            "appid": 620,
                            "name": "Portal 2",
                            "playtime_forever": "a lot",
                            "rtime_last_played": -1,
                        }
                    ],
                }
            },
        )
    )

    item = (await provider.fetch_library())[0]

    assert (item.playtime_minutes, item.last_played_at) == (None, None)


@respx.mock
async def test_an_entry_without_an_appid_is_malformed(provider: SteamProvider) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(
            200, json={"response": {"game_count": 1, "games": [{"name": "Nameless"}]}}
        )
    )

    with pytest.raises(MalformedResponseError, match="appid"):
        await provider.fetch_library()


@respx.mock
async def test_working_credentials_say_nothing(provider: SteamProvider) -> None:
    """The answer is the absence of an exception; a boolean could not carry the reason."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded_json("owned_games_empty.json"))
    )

    assert await provider.validate_credentials() is None
