import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api.accounts import MASK
from ludarium.config import Settings
from ludarium.crypto import get_cipher
from ludarium.models import Account, AppUser, Provider
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
def test_the_same_account_cannot_be_connected_twice(signed_in: TestClient) -> None:
    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(200, json=recorded("owned_games.json"))
    )
    assert connect(signed_in).status_code == 201

    response = connect(signed_in)

    assert response.status_code == 409


def test_an_unknown_provider_is_a_404(signed_in: TestClient) -> None:
    response = connect(signed_in, provider="epic")

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
