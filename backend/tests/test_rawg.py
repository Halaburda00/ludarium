import logging
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from ludarium.providers import (
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderUnavailableError,
    RateLimitedError,
    RawgClient,
)
from ludarium.providers import rawg as rawg_module
from ludarium.providers.rawg import steam_appids

# Shapes from RAWG's OpenAPI document; the values are invented. RAWG data may
# not be redistributed, so nothing here was recorded from the live API.
KEY = "not-a-real-rawg-key"
GAMES_URL = f"{rawg_module.RAWG_API}/games"


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rawg_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(rawg_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def rawg() -> AsyncIterator[RawgClient]:
    async with httpx.AsyncClient() as client:
        yield RawgClient(KEY, client)


@respx.mock
async def test_a_search_asks_precisely_for_games_and_returns_the_candidates(
    rawg: RawgClient,
) -> None:
    route = respx.get(GAMES_URL).mock(
        return_value=httpx.Response(
            200, json={"count": 1, "results": [{"id": 3328, "slug": "the-witcher-3-wild-hunt"}]}
        )
    )

    found = await rawg.search("The Witcher 3: Wild Hunt")

    assert found == [{"id": 3328, "slug": "the-witcher-3-wild-hunt"}]
    params = route.calls.last.request.url.params
    assert params["search"] == "The Witcher 3: Wild Hunt"
    assert (params["search_precise"], params["exclude_additions"]) == ("true", "true")
    assert (params["page_size"], params["key"]) == (str(rawg_module.CANDIDATES), KEY)


@respx.mock
async def test_stores_and_the_full_record_are_read(rawg: RawgClient) -> None:
    respx.get(f"{GAMES_URL}/3328/stores").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 1, "url": "https://x"}]})
    )
    respx.get(f"{GAMES_URL}/3328").mock(
        return_value=httpx.Response(200, json={"id": 3328, "metacritic": 92})
    )

    assert await rawg.stores(3328) == [{"id": 1, "url": "https://x"}]
    assert await rawg.game(3328) == {"id": 3328, "metacritic": 92}


@respx.mock
async def test_a_game_rawg_no_longer_has_is_none_and_sold_nowhere(rawg: RawgClient) -> None:
    respx.get(f"{GAMES_URL}/3328").mock(return_value=httpx.Response(404, json={}))
    respx.get(f"{GAMES_URL}/3328/stores").mock(return_value=httpx.Response(404, json={}))

    assert await rawg.game(3328) is None
    assert await rawg.stores(3328) == []


@respx.mock
async def test_a_404_on_a_search_is_still_malformed(rawg: RawgClient) -> None:
    """Only a lookup by id can find its game gone; the search endpoint itself cannot be."""

    respx.get(GAMES_URL).mock(return_value=httpx.Response(404, json={}))

    with pytest.raises(MalformedResponseError, match="404"):
        await rawg.search("Prey")


@respx.mock
async def test_a_record_about_another_game_is_refused(rawg: RawgClient) -> None:
    respx.get(f"{GAMES_URL}/3328").mock(return_value=httpx.Response(200, json={"id": 1}))

    with pytest.raises(MalformedResponseError, match="another game"):
        await rawg.game(3328)


def test_a_steam_link_names_its_appid_and_nothing_else_does() -> None:
    stores = [
        {"url": "https://store.steampowered.com/app/292030/The_Witcher_3_Wild_Hunt/"},
        {"url": "http://store.steampowered.com/app/499450"},
        {"url": "https://store.steampowered.com/app/2920301x/"},
        {"url": "https://store.steampowered.com/sub/124923/"},
        {"url": "https://www.gog.com/game/the_witcher_3_wild_hunt"},
        {"url": None},
    ]

    assert steam_appids(stores) == {"292030", "499450"}


@respx.mock
async def test_an_outage_is_retried_then_reported(rawg: RawgClient) -> None:
    route = respx.get(GAMES_URL).mock(return_value=httpx.Response(502))

    with pytest.raises(ProviderUnavailableError, match="502"):
        await rawg.search("Prey")

    assert route.call_count == rawg_module.RETRY_ATTEMPTS


@respx.mock
async def test_a_429_is_the_month_spent_and_not_retried(rawg: RawgClient) -> None:
    route = respx.get(GAMES_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(RateLimitedError):
        await rawg.search("Prey")

    assert route.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_a_refused_key_is_invalid_credentials(rawg: RawgClient, status: int) -> None:
    respx.get(GAMES_URL).mock(return_value=httpx.Response(status))

    with pytest.raises(InvalidCredentialsError):
        await rawg.search("Prey")


@pytest.mark.parametrize(
    "body", [{"results": None}, {"results": [1]}, {}, [], "not json"], ids=repr
)
@respx.mock
async def test_a_body_that_is_not_a_list_of_results_is_malformed(
    rawg: RawgClient, body: object
) -> None:
    response = (
        httpx.Response(200, text=body) if isinstance(body, str) else httpx.Response(200, json=body)
    )
    respx.get(GAMES_URL).mock(return_value=response)

    with pytest.raises(MalformedResponseError):
        await rawg.search("Prey")


@respx.mock
async def test_the_key_is_in_no_error_and_no_log(
    rawg: RawgClient, caplog: pytest.LogCaptureFixture
) -> None:
    """It travels in the query string, so a message that quoted the URL would leak it (rule 7)."""

    respx.get(GAMES_URL).mock(side_effect=httpx.ConnectError(f"refused: {GAMES_URL}?key={KEY}"))

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderUnavailableError) as caught:
        await rawg.search("Prey")

    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert KEY not in caplog.text
    assert KEY not in repr(rawg)
