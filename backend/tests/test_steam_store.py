import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ludarium.providers import MalformedResponseError, ProviderUnavailableError, RateLimitedError
from ludarium.providers import steam_store as store_module
from ludarium.providers.steam_store import SteamStoreClient

FIXTURES = Path(__file__).parent / "fixtures" / "steam_store"
GET_ITEMS_URL = f"{store_module.STORE_API}{store_module.GET_ITEMS}"
# Everything `items.json` was recorded for, in the order it was asked.
RECORDED = ["1611740", "35420", "235900", "594650", "770720", "378649", "323180", "35020", "227700"]


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def asked(route: respx.Route) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(route.calls.last.request.url.params["input_json"])
    return decoded


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(store_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def store() -> AsyncIterator[SteamStoreClient]:
    async with httpx.AsyncClient() as client:
        yield SteamStoreClient(client)


@respx.mock
async def test_every_app_the_store_knows_comes_back_keyed_as_asked(
    store: SteamStoreClient,
) -> None:
    respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(200, json=recorded("items.json")))

    found = await store.items(RECORDED)

    # Firefall is delisted: `success` 15, and left out rather than returned empty.
    assert list(found) == RECORDED[:-1]
    assert found["1611740"]["type"] == 12
    assert found["1611740"]["related_items"] == {"parent_appid": 671860}


@respx.mock
async def test_the_request_names_a_country(store: SteamStoreClient) -> None:
    """Without one the store answers every request with nothing (see `without_context`)."""

    route = respx.get(GET_ITEMS_URL).mock(
        return_value=httpx.Response(200, json=recorded("items.json"))
    )

    await store.items(RECORDED)

    request = asked(route)
    assert request["context"]["country_code"] == "US"
    assert request["ids"] == [{"appid": int(appid)} for appid in RECORDED]


@respx.mock
async def test_the_answer_a_request_without_a_country_gets_is_refused(
    store: SteamStoreClient,
) -> None:
    """Recorded from the live endpoint. Returned as `{}` it would cache nine false absences."""

    respx.get(GET_ITEMS_URL).mock(
        return_value=httpx.Response(200, json=recorded("without_context.json"))
    )

    with pytest.raises(MalformedResponseError, match="store_items"):
        await store.items(RECORDED)


@respx.mock
async def test_an_answer_that_leaves_an_app_out_is_refused(store: SteamStoreClient) -> None:
    body = recorded("items.json")
    body["response"]["store_items"] = body["response"]["store_items"][:-1]
    respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(MalformedResponseError, match="1 of 9 apps unanswered"):
        await store.items(RECORDED)


@respx.mock
async def test_an_answer_about_an_app_nobody_asked_about_is_refused(
    store: SteamStoreClient,
) -> None:
    respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(200, json=recorded("items.json")))

    with pytest.raises(MalformedResponseError, match="not asked"):
        await store.items(RECORDED[1:])


@pytest.mark.parametrize("item", [{"success": 1}, {"id": True, "success": 1}, "1611740"])
@respx.mock
async def test_an_item_that_names_no_app_is_refused(store: SteamStoreClient, item: object) -> None:
    respx.get(GET_ITEMS_URL).mock(
        return_value=httpx.Response(200, json={"response": {"store_items": [item]}})
    )

    with pytest.raises(MalformedResponseError):
        await store.items(["1611740"])


@pytest.mark.parametrize(
    "body", [{"response": []}, [], "not json"], ids=["no-object", "a-list", "not-json"]
)
@respx.mock
async def test_a_body_that_is_not_the_documented_shape_is_refused(
    store: SteamStoreClient, body: object
) -> None:
    content = body if isinstance(body, str) else json.dumps(body)
    respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(200, content=content))

    with pytest.raises(MalformedResponseError):
        await store.items(["1611740"])


@respx.mock
async def test_a_server_error_is_retried_and_then_reported_as_an_outage(
    store: SteamStoreClient,
) -> None:
    route = respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(502))

    with pytest.raises(ProviderUnavailableError, match="502"):
        await store.items(["1611740"])

    assert route.call_count == store_module.RETRY_ATTEMPTS


@respx.mock
async def test_a_server_error_that_clears_is_an_answer(store: SteamStoreClient) -> None:
    respx.get(GET_ITEMS_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=recorded("items.json"))]
    )

    assert "1611740" in await store.items(RECORDED)


@respx.mock
async def test_a_transport_failure_is_an_outage(store: SteamStoreClient) -> None:
    respx.get(GET_ITEMS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    with pytest.raises(ProviderUnavailableError, match="ConnectTimeout"):
        await store.items(["1611740"])


@respx.mock
async def test_a_rate_limit_is_reported_and_never_retried(store: SteamStoreClient) -> None:
    route = respx.get(GET_ITEMS_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
    )

    with pytest.raises(RateLimitedError) as caught:
        await store.items(["1611740"])

    assert caught.value.retry_after == 30.0
    assert route.call_count == 1


@respx.mock
async def test_a_url_the_store_will_not_take_is_not_retried(store: SteamStoreClient) -> None:
    """The 400 an oversized query string gets, measured at 300 appids."""

    route = respx.get(GET_ITEMS_URL).mock(return_value=httpx.Response(400))

    with pytest.raises(MalformedResponseError, match="400"):
        await store.items(["1611740"])

    assert route.call_count == 1


@respx.mock
async def test_nothing_to_ask_asks_nothing(store: SteamStoreClient) -> None:
    route = respx.get(GET_ITEMS_URL)

    assert await store.items([]) == {}
    assert not route.called


async def test_more_apps_than_a_url_holds_are_refused_before_asking(
    store: SteamStoreClient,
) -> None:
    appids = [str(appid) for appid in range(1_000_000, 1_000_000 + store_module.MAX_BATCH + 1)]

    with pytest.raises(ValueError, match="at most"):
        await store.items(appids)


async def test_an_app_named_twice_counts_once_against_the_batch(store: SteamStoreClient) -> None:
    with respx.mock:
        route = respx.get(GET_ITEMS_URL).mock(
            return_value=httpx.Response(200, json=recorded("items.json"))
        )
        await store.items(RECORDED + RECORDED)

    assert len(asked(route)["ids"]) == len(RECORDED)


async def test_an_id_that_is_not_an_appid_is_refused_before_asking(
    store: SteamStoreClient,
) -> None:
    with pytest.raises(ValueError, match="digits"):
        await store.items(["1611740", "gog:1495134320"])
