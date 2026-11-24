import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import TEST_PASSWORD, TEST_USERNAME, make_account, make_entitlement, make_work
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api.accounts import MASK
from ludarium.config import Settings
from ludarium.crypto import get_cipher
from ludarium.enums import SyncErrorKind, SyncStatus, SyncTrigger
from ludarium.models import Account, AppUser, EntitlementWork, Provider, SyncRun
from ludarium.providers import epic as epic_module
from ludarium.providers import steam as steam_module

FIXTURES = Path(__file__).parent / "fixtures" / "steam"
OWNED_GAMES_URL = f"{steam_module.STEAM_API}{steam_module.OWNED_GAMES}"
API_KEY = "0123456789ABCDEF-not-a-real-key"
STEAM_ID = "76561197960287930"


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    response = client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return client


def connect(client: TestClient, **overrides: Any) -> httpx.Response:
    payload = {
        "provider": "steam",
        "external_account_id": STEAM_ID,
        "label": "Main",
        "credentials": API_KEY,
    } | overrides
    response: httpx.Response = client.post("/api/accounts", json=payload)
    return response


def stored_accounts(settings: Settings) -> list[tuple[int, bytes | None]]:
    engine = create_engine(settings.database_url.replace("+aiosqlite", ""))
    try:
        with engine.connect() as connection:
            return [
                (row[0], row[1])
                for row in connection.execute(select(Account.id, Account.credentials_encrypted))
            ]
    finally:
        engine.dispose()


@respx.mock
def test_a_working_key_connects_the_account(signed_in: TestClient) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )

    response = connect(signed_in)

    assert response.status_code == 201
    body = response.json()
    assert body["provider"] == "steam"
    assert body["external_account_id"] == STEAM_ID
    assert body["credentials"] == MASK


@respx.mock
def test_the_key_is_checked_against_the_platform_before_anything_is_written(
    signed_in: TestClient, settings: Settings
) -> None:
    """A wrong key is a 400 while the page is still open, not a failed run found later.

    And it leaves nothing behind: an account row written first and validated
    afterwards would have to be cleaned up by whoever noticed.
    """

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(403, text=(FIXTURES / "unauthorised.html").read_text())
    )

    response = connect(signed_in)

    assert response.status_code == 400
    assert stored_accounts(settings) == []


@respx.mock
def test_a_private_profile_is_not_reported_as_a_bad_key(signed_in: TestClient) -> None:
    """Different fix, so a different message: rotating a working key helps nobody."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("private_profile.json"))
    )

    response = connect(signed_in)

    assert response.status_code == 400
    assert "private" in response.json()["detail"]


@respx.mock
def test_an_outage_is_not_the_users_fault(signed_in: TestClient, settings: Settings) -> None:
    """502, not 400. Told their key is bad, someone rotates one that was working."""

    respx.get(OWNED_GAMES_URL).mock(return_value=httpx.Response(503))

    response = connect(signed_in)

    assert response.status_code == 502
    assert stored_accounts(settings) == []


@respx.mock
def test_the_key_is_encrypted_at_rest(signed_in: TestClient, settings: Settings) -> None:
    """Rule 7, checked against the bytes on disk rather than against the model."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )

    connect(signed_in)

    stored = stored_accounts(settings)
    assert len(stored) == 1
    ciphertext = stored[0][1]
    assert ciphertext is not None
    assert API_KEY.encode() not in ciphertext
    assert get_cipher().decrypt(ciphertext) == API_KEY


@respx.mock
def test_the_listing_masks_the_key(signed_in: TestClient) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )
    connect(signed_in)

    body = signed_in.get("/api/accounts").json()

    assert [account["credentials"] for account in body] == [MASK]
    assert API_KEY not in json.dumps(body)


def test_the_mask_says_nothing_about_the_key() -> None:
    """Fixed, not derived: a mask that mirrors the length is a fact about the secret."""

    assert "•" * len(MASK) == MASK


@respx.mock
def test_connecting_an_account_again_replaces_its_key_and_keeps_the_account(
    signed_in: TestClient, settings: Settings
) -> None:
    """How a Steam key is rotated, and how an Epic sign-in that ended comes back."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )
    first = connect(signed_in)
    assert first.status_code == 201

    again = connect(signed_in, credentials="FEDCBA9876543210-another-key")

    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    ((_, stored),) = stored_accounts(settings)
    assert stored is not None
    assert get_cipher().decrypt(stored) == "FEDCBA9876543210-another-key"


def test_an_unknown_provider_is_a_404(signed_in: TestClient) -> None:
    response = connect(signed_in, provider="stadia")

    assert response.status_code == 404


def test_a_provider_with_no_library_client_says_so(signed_in: TestClient) -> None:
    """`manual` is a real provider row with real entitlements and nothing to ask."""

    response = connect(signed_in, provider="manual")

    assert response.status_code == 400
    assert "library client" in response.json()["detail"]


def test_connecting_needs_a_session(client: TestClient) -> None:
    assert connect(client).status_code == 401
    assert client.get("/api/accounts").status_code == 401


async def test_the_account_belongs_to_the_signed_in_user(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """ADR-0003: the column is real and populated even though the UI is single-tenant."""

    with respx.mock:
        respx.get(OWNED_GAMES_URL).mock(
            return_value=httpx.Response(200, json=recorded("owned_games.json"))
        )
        connect(signed_in)

    account = await session.scalar(select(Account))
    assert account is not None
    assert account.user_id == 1


@respx.mock
def test_being_asked_to_slow_down_is_its_own_answer(
    signed_in: TestClient, settings: Settings
) -> None:
    """429 out as well as in. Retrying a rate limit immediately is how it becomes a ban."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(429, text=(FIXTURES / "rate_limited.html").read_text())
    )

    response = connect(signed_in)

    assert response.status_code == 429
    assert stored_accounts(settings) == []


async def test_the_listing_shows_only_the_signed_in_users_accounts(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """ADR-0003 again, on the read side. One user in the UI is not a filter in a query."""

    stranger = AppUser(username="somebody-else", password_hash="not-a-hash")
    session.add(stranger)
    await session.flush()
    provider = await session.scalar(select(Provider).where(Provider.key == "steam"))
    assert provider is not None
    session.add(
        Account(
            user_id=stranger.id,
            provider_id=provider.id,
            external_account_id="76561197960287999",
            label="Not mine",
        )
    )
    await session.commit()

    body = signed_in.get("/api/accounts").json()

    assert [account["label"] for account in body] == []


@respx.mock
def test_the_platforms_own_backoff_reaches_the_caller(signed_in: TestClient) -> None:
    """`Retry-After` passed through rather than dropped at the provider boundary.

    Without it the frontend has nothing to base a retry on but a guess, and a
    guessed retry into a rate limit is how a rate limit becomes a ban.
    """

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "42"},
            text=(FIXTURES / "rate_limited.html").read_text(),
        )
    )

    response = connect(signed_in)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"


@respx.mock
def test_an_epic_account_is_connected_by_the_code_its_sign_in_page_showed(
    signed_in: TestClient, settings: Settings
) -> None:
    """The code is spent on the way in; what is kept is the refresh token it bought."""

    oauth = json.loads((FIXTURES.parent / "epic" / "oauth_token.json").read_text())
    respx.post(epic_module.OAUTH).mock(return_value=httpx.Response(200, json=oauth))

    response = connect(
        signed_in, provider="epic", external_account_id="", credentials="not-a-real-code"
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["provider"], body["external_account_id"]) == ("epic", oauth["account_id"])
    ((_, stored),) = stored_accounts(settings)
    assert stored is not None
    assert get_cipher().decrypt(stored) == oauth["refresh_token"]
    assert oauth["refresh_token"] not in response.text


@respx.mock
def test_a_spent_epic_code_is_a_400_that_says_to_sign_in_again(signed_in: TestClient) -> None:
    respx.post(epic_module.OAUTH).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )

    response = connect(
        signed_in, provider="epic", external_account_id="", credentials="not-a-real-code"
    )

    assert response.status_code == 400
    assert "fresh one" in response.json()["detail"]


@respx.mock
def test_connecting_an_account_a_report_made_takes_it_over(signed_in: TestClient) -> None:
    report = {
        "version": 1,
        "reporter": "manual",
        "account": {"provider": "steam", "external_account_id": STEAM_ID, "label": "Imported"},
        "items": [{"provider_item_id": "620", "title": "Portal 2"}],
    }
    assert signed_in.post("/api/ingest", json=report).status_code == 200
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )

    # The store step the sync queues after itself; its answer is not this test's.
    respx.get(url__startswith="https://api.steampowered.com/IStoreBrowseService/").mock(
        return_value=httpx.Response(503)
    )
    assert connect(signed_in).status_code == 200

    # Connected now, so its platform syncs it, and a report about it is refused
    # as about any connected account (ADR-0035).
    assert signed_in.post("/api/sync/steam").status_code == 200
    assert signed_in.post("/api/ingest", json=report).status_code == 409


async def failed_steam_account(session: AsyncSession) -> Account:
    """A connected Steam account whose last run failed on its credential."""

    account = await make_account(session, external_account_id=STEAM_ID)
    account.status, account.last_error = SyncStatus.FAILED, "steam rejected the key"
    session.add(
        SyncRun(
            provider_id=account.provider_id,
            account_id=account.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.FAILED,
            error_text="steam rejected the key",
            error_kind=SyncErrorKind.CREDENTIALS,
        )
    )
    provider = await session.get_one(Provider, account.provider_id)
    provider.status, provider.last_error = SyncStatus.FAILED, "steam rejected the key"
    await session.commit()
    return account


async def test_an_account_says_its_platform_whether_it_was_imported_and_who_can_fix_it(
    signed_in: TestClient, session: AsyncSession
) -> None:
    account = await failed_steam_account(session)

    [listed] = signed_in.get("/api/accounts").json()

    assert listed["id"] == account.id
    assert (listed["provider_name"], listed["is_derived"]) == ("Steam", False)
    assert (listed["status"], listed["error_kind"]) == ("failed", "credentials")


async def test_an_account_can_be_renamed(signed_in: TestClient, session: AsyncSession) -> None:
    account = await failed_steam_account(session)

    response = signed_in.patch(f"/api/accounts/{account.id}", json={"label": "  Family PC  "})

    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Family PC"
    assert response.json()["is_active"] is True


async def test_switching_an_account_off_stops_its_syncs_and_its_alarm_but_keeps_its_games(
    signed_in: TestClient, session: AsyncSession
) -> None:
    account = await failed_steam_account(session)
    work = await make_work(session)
    entitlement = await make_entitlement(session, account)
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    await session.commit()

    response = signed_in.patch(f"/api/accounts/{account.id}", json={"is_active": False})

    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    # Not asked to sync, by hand or on the schedule.
    assert signed_in.post("/api/sync/steam").status_code == 404
    # The platform no longer reports a failure nobody is asked to fix.
    providers = signed_in.get("/api/sync/runs").json()["providers"]
    steam = next(provider for provider in providers if provider["key"] == "steam")
    assert (steam["status"], steam["last_error"]) == ("pending", None)
    # Switching off is not removing (rule 1).
    titles = [work["title"] for work in signed_in.get("/api/works").json()["works"]]
    assert titles == ["The Witcher 3: Wild Hunt"]

    back = signed_in.patch(f"/api/accounts/{account.id}", json={"is_active": True})
    assert back.json()["is_active"] is True
    steam = next(
        provider
        for provider in signed_in.get("/api/sync/runs").json()["providers"]
        if provider["key"] == "steam"
    )
    assert steam["status"] == "failed"


@pytest.mark.parametrize(
    "body", [{}, {"label": "   "}, {"label": "x" * 257}, {"is_active": None}, {"provider": "epic"}]
)
async def test_an_update_that_says_nothing_or_nonsense_is_refused(
    signed_in: TestClient, session: AsyncSession, body: dict[str, Any]
) -> None:
    account = await failed_steam_account(session)

    assert signed_in.patch(f"/api/accounts/{account.id}", json=body).status_code == 422


async def test_an_account_that_is_not_there_is_a_404(signed_in: TestClient) -> None:
    assert signed_in.patch("/api/accounts/999", json={"label": "Mine"}).status_code == 404
