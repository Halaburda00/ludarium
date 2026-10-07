import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient

from ludarium.enums import ItemKind, OwnershipType
from ludarium.providers import (
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderUnavailableError,
)
from ludarium.providers import gog as gog_module
from ludarium.providers.gog import GogProvider, sign_in

FIXTURES = Path(__file__).parent / "fixtures" / "gog"
USER = "48000000000000001"
STORED = "the-refresh-token-we-hold"
STATS = gog_module.STATS.format(username="fixture_user")


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def token(**overrides: Any) -> httpx.Response:
    return httpx.Response(200, json={**recorded("oauth_token.json"), **overrides})


def paged(prefix: str) -> Any:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=recorded(f"{prefix}_{request.url.params['page']}.json"))

    return answer


def mount(oauth: httpx.Response | None = None) -> dict[str, respx.Route]:
    return {
        "oauth": respx.post(gog_module.TOKEN).mock(return_value=oauth or token()),
        "products": respx.get(gog_module.PRODUCTS).mock(side_effect=paged("products_page")),
        "owned": respx.get(gog_module.OWNED).mock(
            return_value=httpx.Response(200, json=recorded("owned.json"))
        ),
        "user": respx.get(gog_module.USER).mock(
            return_value=httpx.Response(200, json=recorded("user.json"))
        ),
        "stats": respx.get(STATS).mock(side_effect=paged("stats_page")),
        "catalog": respx.get(gog_module.CATALOG).mock(
            return_value=httpx.Response(200, json=recorded("catalog_ids.json"))
        ),
        "v2": respx.get(gog_module.CATALOG_V2.format(id="1256837418")).mock(
            return_value=httpx.Response(200, json=recorded("v2_1256837418.json"))
        ),
    }


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gog_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(gog_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def gog(http: httpx.AsyncClient) -> GogProvider:
    return GogProvider(STORED, USER, http)


@pytest.mark.parametrize(
    "pasted",
    [
        "the-code",
        "  the-code  ",
        "https://embed.gog.com/on_login_success?origin=client&code=the-code",
    ],
)
@respx.mock
async def test_a_code_or_the_page_s_address_buys_the_account_and_its_refresh_token(
    http: httpx.AsyncClient, pasted: str
) -> None:
    routes = mount()

    signed = await sign_in(http, pasted)

    assert (signed.user_id, signed.refresh_token) == (USER, "fixture-refresh-token")
    request = routes["oauth"].calls.last.request
    # In the form body, never in the URL (rule 7).
    assert not request.url.query
    form = parse_qs(request.content.decode())
    assert form["code"] == ["the-code"]
    assert form["grant_type"] == ["authorization_code"]
    assert form["redirect_uri"] == [gog_module.REDIRECT_URI]
    assert "fixture-refresh-token" not in repr(signed)


@respx.mock
async def test_a_spent_code_says_to_sign_in_again(http: httpx.AsyncClient) -> None:
    mount(httpx.Response(400, json=recorded("oauth_invalid_grant.json")))

    with pytest.raises(InvalidCredentialsError, match="sign in again"):
        await sign_in(http, "spent")


@respx.mock
async def test_the_library_is_every_game_and_add_on_with_its_playtime(gog: GogProvider) -> None:
    routes = mount()

    fetched = await gog.fetch_library()

    items = {item.provider_item_id: item for item in fetched.items}
    # Four games over two pages, and the one add-on among the owned ids. The
    # pack and the id the catalogue does not know are not library items, and
    # are not counted as lost either: a short count would stop every sweep.
    assert set(items) == {"1495134320", "5", "1423049311", "1083619800", "1256837418"}
    assert fetched.skipped == 0 and fetched.complete
    witcher = items["1495134320"]
    assert (witcher.title, witcher.item_kind, witcher.ownership_type) == (
        "The Witcher 3: Wild Hunt — Remastered",
        ItemKind.GAME,
        OwnershipType.OWNED,
    )
    assert witcher.playtime_minutes == 893
    assert witcher.last_played_at == datetime(2026, 10, 1, 18, 15, tzinfo=UTC)
    # Statistics given as an empty list are no statistics.
    assert (items["5"].playtime_minutes, items["5"].last_played_at) == (None, None)
    liberty = items["1256837418"]
    assert (liberty.title, liberty.item_kind, liberty.parent_item_id) == (
        "Cyberpunk 2077: Phantom Liberty",
        ItemKind.DLC,
        "1423049311",
    )
    # Only the ids the product list did not name are looked up.
    asked = routes["catalog"].calls.last.request.url.params["ids"].split(",")
    assert sorted(asked) == ["1084485308", "1085477296", "1256837418"]
    # The catalogue is public, and the token stays with GOG's own hosts.
    for name in ("catalog", "v2"):
        assert "authorization" not in routes[name].calls.last.request.headers


@respx.mock
async def test_the_refresh_token_is_kept_as_it_is(gog: GogProvider) -> None:
    routes = mount()

    await gog.fetch_library()

    form = parse_qs(routes["oauth"].calls.last.request.content.decode())
    assert form["refresh_token"] == [STORED]
    assert gog.renewed_secret is None


@respx.mock
async def test_an_ended_sign_in_is_the_user_s_to_fix(gog: GogProvider) -> None:
    mount(httpx.Response(400, json=recorded("oauth_invalid_grant.json")))

    with pytest.raises(InvalidCredentialsError, match="connect the account again"):
        await gog.fetch_library()


@respx.mock
async def test_a_token_for_another_account_is_refused(gog: GogProvider) -> None:
    mount(token(user_id="48000000000000999"))

    with pytest.raises(InvalidCredentialsError, match="different account"):
        await gog.fetch_library()


@respx.mock
async def test_a_refused_token_sent_to_the_sign_in_page_is_a_credential_problem(
    gog: GogProvider,
) -> None:
    routes = mount()
    # Measured: a bearer GOG does not accept is redirected, not answered 401.
    routes["products"].mock(
        return_value=httpx.Response(302, headers={"Location": "/en##openlogin"})
    )

    with pytest.raises(InvalidCredentialsError, match="refused a fresh sign-in"):
        await gog.fetch_library()


@respx.mock
async def test_statistics_that_fail_fail_the_library(gog: GogProvider) -> None:
    routes = mount()
    routes["stats"].mock(return_value=httpx.Response(503))

    with pytest.raises(ProviderUnavailableError):
        await gog.fetch_library()
    # Retried before giving up.
    assert routes["stats"].call_count == gog_module.RETRY_ATTEMPTS


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("products", {"products": "not a list", "totalPages": 1}),
        ("products", {"products": [{"title": "No id"}], "totalPages": 1}),
        ("owned", {"owned": ["5"]}),
        ("user", {"username": "../somewhere?else"}),
        ("stats", {"pages": 1}),
    ],
)
@respx.mock
async def test_an_answer_it_cannot_read_fails_the_library(
    gog: GogProvider, route: str, body: dict[str, Any]
) -> None:
    routes = mount()
    routes[route].mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(MalformedResponseError):
        await gog.fetch_library()


@respx.mock
async def test_no_token_reaches_an_error_message(gog: GogProvider) -> None:
    routes = mount()
    routes["products"].mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(ProviderUnavailableError) as raised:
        await gog.fetch_library()

    message = str(raised.value)
    assert STORED not in message and "fixture-access-token" not in message
    assert raised.value.__cause__ is None


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    response = client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return client


@respx.mock
def test_a_connected_gog_account_syncs_into_the_library(signed_in: TestClient) -> None:
    mount()

    connected = signed_in.post(
        "/api/accounts",
        json={"provider": "gog", "credentials": "the-code", "label": "Main"},
    )
    assert connected.status_code == 201, connected.text
    assert connected.json()["external_account_id"] == USER

    synced = signed_in.post("/api/sync/gog").json()
    assert [(run["status"], run["items_seen"]) for run in synced["runs"]] == [("success", 5)]

    works = signed_in.get("/api/works").json()["works"]
    by_title = {work["title"]: work for work in works}
    # The add-on folds under its game; GOG gives no store link.
    assert set(by_title) == {
        "The Witcher 3: Wild Hunt — Remastered",
        "Freespace 2",
        "Cyberpunk 2077",
        "The Evil Within 2",
    }
    assert by_title["Cyberpunk 2077"]["addon_count"] == 1
    assert by_title["The Witcher 3: Wild Hunt — Remastered"]["playtime_minutes"] == 893
    copy = by_title["Freespace 2"]["entitlements"][0]
    assert (copy["provider"], copy["provider_name"], copy["store_url"]) == ("gog", "GOG", None)
