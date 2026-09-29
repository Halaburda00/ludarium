import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from ludarium.enums import ItemKind, OwnershipType
from ludarium.providers import (
    InvalidCredentialsError,
    MalformedResponseError,
    ProviderUnavailableError,
)
from ludarium.providers import epic as epic_module
from ludarium.providers.epic import EpicProvider, sign_in

FIXTURES = Path(__file__).parent / "fixtures" / "epic"
ACCOUNT = "0123456789abcdef0123456789abcdef"
STORED = "eg1~the-refresh-token-we-hold"


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


CATALOG = recorded("catalog_by_namespace.json")


def token(**overrides: Any) -> httpx.Response:
    return httpx.Response(200, json={**recorded("oauth_token.json"), **overrides})


def answer_library(request: httpx.Request) -> httpx.Response:
    page = 2 if request.url.params.get("cursor") == "cursor-for-page-2" else 1
    return httpx.Response(200, json=recorded(f"library_page_{page}.json"))


def answer_catalog(request: httpx.Request) -> httpx.Response:
    namespace = request.url.path.split("/namespace/")[1].split("/")[0]
    asked = request.url.params.get_list("id")
    known = CATALOG.get(namespace, {})
    return httpx.Response(200, json={item: known[item] for item in asked if item in known})


def mount(oauth: httpx.Response | None = None) -> dict[str, respx.Route]:
    return {
        "oauth": respx.post(epic_module.OAUTH).mock(return_value=oauth or token()),
        "library": respx.get(epic_module.LIBRARY).mock(side_effect=answer_library),
        "catalog": respx.get(url__startswith=epic_module.CATALOG).mock(side_effect=answer_catalog),
    }


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epic_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(epic_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def epic(http: httpx.AsyncClient) -> EpicProvider:
    return EpicProvider(STORED, ACCOUNT, http)


@respx.mock
async def test_a_code_is_exchanged_in_a_form_body_for_the_account_and_its_refresh_token(
    http: httpx.AsyncClient,
) -> None:
    routes = mount()

    signed = await sign_in(http, ' "0a1b2c3d4e5f60718293a4b5c6d7e8f9" ')

    assert (signed.account_id, signed.display_name) == (ACCOUNT, "Someone")
    assert signed.refresh_token == "eg1~not-a-real-refresh-token"
    request = routes["oauth"].calls.last.request
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["authorization_code"],
        "code": ["0a1b2c3d4e5f60718293a4b5c6d7e8f9"],
        "token_type": ["eg1"],
    }
    assert "0a1b2c3d" not in str(request.url)
    assert request.headers["authorization"].startswith("basic ")
    assert "not-a-real" not in repr(signed)


@respx.mock
async def test_a_spent_code_says_to_sign_in_again(http: httpx.AsyncClient) -> None:
    """Recorded: Epic's answer to a code that had already expired."""

    mount(httpx.Response(400, json=recorded("oauth_code_not_found.json")))

    with pytest.raises(InvalidCredentialsError, match="fresh one"):
        await sign_in(http, "0a1b2c3d4e5f60718293a4b5c6d7e8f9")


@respx.mock
async def test_the_library_is_every_page_one_item_per_catalogue_entry_in_english(
    epic: EpicProvider,
) -> None:
    routes = mount()

    fetched = await epic.fetch_library()
    items = fetched.items

    # Neither the build listed twice nor the plugin left out below was lost.
    assert fetched.skipped == 0
    assert [(item.title, item.item_kind) for item in items] == [
        ("Gone Home", ItemKind.GAME),
        # Listed twice by the library, once per build: one entitlement.
        ("Sid Meier's Civilization VI", ItemKind.GAME),
        # The Unreal Marketplace plugin is left out: not a library item.
        ("Civilization VI : Rise and Fall", ItemKind.DLC),
        ("Unreal Engine", ItemKind.TOOL),
        ("Minit", ItemKind.GAME),
    ]
    assert routes["library"].call_count == 2
    gone_home = items[0]
    assert gone_home.provider_item_id == "48171393707541359f3a7dd7257b2757"
    assert gone_home.raw["namespace"] == "52326e805bac4619a4a8fac165363a42"
    assert gone_home.acquired_at is not None and gone_home.acquired_at.tzinfo is not None
    # Minit was a weekly giveaway: claimed, so owned (#64).
    assert {item.ownership_type for item in items} == {OwnershipType.OWNED}
    asked = routes["catalog"].calls.last.request.url.params
    assert (asked["locale"], asked["country"]) == ("en", "US")


@respx.mock
async def test_each_use_leaves_the_replacement_token_for_the_sync_to_keep(
    epic: EpicProvider,
) -> None:
    routes = mount()

    await epic.fetch_library()

    assert epic.renewed_secret == "eg1~not-a-real-refresh-token"
    sent = parse_qs(routes["oauth"].calls.last.request.content.decode())
    assert sent["refresh_token"] == [STORED]
    assert STORED not in repr(epic)


@respx.mock
async def test_an_ended_sign_in_is_the_user_s_to_fix(epic: EpicProvider) -> None:
    mount(httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(InvalidCredentialsError, match="connect the account again"):
        await epic.fetch_library()


@respx.mock
async def test_a_token_for_another_account_is_refused(epic: EpicProvider) -> None:
    mount(token(account_id="ffffffffffffffffffffffffffffffff"))

    with pytest.raises(InvalidCredentialsError, match="different account"):
        await epic.fetch_library()


@respx.mock
async def test_an_item_the_catalogue_does_not_know_keeps_its_app_name(
    epic: EpicProvider,
) -> None:
    mount()
    respx.get(url__startswith=epic_module.CATALOG).mock(return_value=httpx.Response(200, json={}))

    items = (await epic.fetch_library()).items

    assert ("Flier", None) in [(item.title, item.item_kind) for item in items]


@pytest.mark.parametrize(
    "record",
    [
        {"namespace": "52326e805bac4619a4a8fac165363a42"},
        {"namespace": "52326e805bac4619a4a8fac165363a42", "catalogItemId": ""},
        {"namespace": None, "catalogItemId": "48171393707541359f3a7dd7257b2757"},
        # A namespace is a path segment in the catalogue URL, so one that is not
        # a plain token would ask another endpoint, bearer token and all.
        {"namespace": "a/../../x", "catalogItemId": "48171393707541359f3a7dd7257b2757"},
        {"namespace": "a?b=1", "catalogItemId": "48171393707541359f3a7dd7257b2757"},
        {"namespace": "", "catalogItemId": "48171393707541359f3a7dd7257b2757"},
        "48171393707541359f3a7dd7257b2757",
    ],
)
@respx.mock
async def test_a_record_it_cannot_read_is_counted_not_raised(
    epic: EpicProvider, record: object
) -> None:
    """One unreadable record costs that record, not the page or the library (#87)."""

    mount()

    def with_one_bad_record(request: httpx.Request) -> httpx.Response:
        page = json.loads(answer_library(request).content)
        if not request.url.params.get("cursor"):
            page["records"].append(record)
        return httpx.Response(200, json=page)

    respx.get(epic_module.LIBRARY).mock(side_effect=with_one_bad_record)

    fetched = await epic.fetch_library()

    assert fetched.skipped == 1
    assert len(fetched.items) == 5


@respx.mock
async def test_a_page_without_a_list_of_records_fails_the_library(epic: EpicProvider) -> None:
    """Nothing in it to count, so nothing to call partial."""

    mount()
    respx.get(epic_module.LIBRARY).mock(return_value=httpx.Response(200, json={"records": {}}))

    with pytest.raises(MalformedResponseError, match="no list of records"):
        await epic.fetch_library()


@respx.mock
async def test_a_page_that_fails_fails_the_library(epic: EpicProvider) -> None:
    """A library read short would mark everything on the missing pages removed (rule 1)."""

    mount()

    def second_page_down(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor"):
            return httpx.Response(503)
        return answer_library(request)

    respx.get(epic_module.LIBRARY).mock(side_effect=second_page_down)

    with pytest.raises(ProviderUnavailableError, match="503"):
        await epic.fetch_library()


@respx.mock
async def test_a_cursor_that_repeats_is_not_followed_for_ever(epic: EpicProvider) -> None:
    mount()
    respx.get(epic_module.LIBRARY).mock(
        return_value=httpx.Response(200, json=recorded("library_page_1.json"))
    )

    with pytest.raises(MalformedResponseError, match="circle"):
        await epic.fetch_library()


@respx.mock
async def test_no_token_reaches_an_error_message(epic: EpicProvider) -> None:
    mount()
    respx.get(epic_module.LIBRARY).mock(
        side_effect=httpx.ConnectError(f"refused, bearer eg1~not-a-real-access-token {STORED}")
    )

    with pytest.raises(ProviderUnavailableError) as caught:
        await epic.fetch_library()

    assert "eg1~" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
