import hashlib
import re
from datetime import timedelta

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.auth import (
    COOKIE_NAME,
    authenticate,
    bootstrap_user,
    hash_password,
    verify_password,
)
from ludarium.models import AppUser, UserSession
from ludarium.models.types import utcnow

# Every route of ours that answers without a cookie. Adding one is a decision,
# which is what `test_every_endpoint_but_the_public_ones_needs_a_session` makes
# visible.
PUBLIC = {
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
}


def sign_in(client: TestClient, *, username: str = TEST_USERNAME, password: str = TEST_PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/api/auth/login", json={"username": username, "password": password})


async def sessions(session: AsyncSession) -> int:
    total = await session.scalar(select(func.count()).select_from(UserSession))
    assert total is not None
    return total


def test_the_right_password_opens_a_session(client: TestClient) -> None:
    response = sign_in(client)

    assert response.status_code == 200
    assert response.json()["username"] == TEST_USERNAME
    assert client.cookies[COOKIE_NAME]


def test_the_cookie_is_httponly_and_same_site_lax(client: TestClient) -> None:
    """The two flags that make it a session cookie rather than a bearer token in a jar."""

    response = sign_in(client)

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    # Plain HTTP on a home network is the ordinary deployment; a `Secure` cookie
    # would simply never be sent back.
    assert "secure" not in cookie


def test_the_cookie_is_secure_over_https(app: FastAPI) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        response = sign_in(client)

    assert "secure" in response.headers["set-cookie"].lower()


def test_a_wrong_password_is_refused(client: TestClient) -> None:
    response = sign_in(client, password="hunter2")

    assert response.status_code == 401
    assert COOKIE_NAME not in client.cookies


def test_a_wrong_username_is_refused_the_same_way(client: TestClient) -> None:
    """Same status and same message: which half was wrong is not the caller's business."""

    wrong_name = sign_in(client, username="somebody-else")
    wrong_password = sign_in(client, password="hunter2")

    assert wrong_name.status_code == 401
    assert wrong_name.json() == wrong_password.json()


def test_no_cookie_means_no_access(client: TestClient) -> None:
    response = client.post("/api/auth/logout")

    assert response.status_code == 401


def test_a_token_nobody_issued_is_refused(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "a-token-i-made-up")

    response = client.post("/api/auth/logout")

    assert response.status_code == 401


async def test_only_the_digest_of_the_token_is_stored(
    client: TestClient, session: AsyncSession
) -> None:
    """The cookie is the secret; the row holds a fingerprint of it.

    A database that leaks — a backup on a NAS share, an export — must not hand
    anyone a working session.
    """

    sign_in(client)
    token = client.cookies[COOKIE_NAME]

    stored = await session.scalar(select(UserSession.token_hash))
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert token not in (stored or "")


async def test_an_expired_session_is_refused(client: TestClient, session: AsyncSession) -> None:
    """`expires_at` is honoured on read, not only written at login."""

    sign_in(client)
    record = await session.scalar(select(UserSession))
    assert record is not None
    record.expires_at = utcnow() - timedelta(seconds=1)
    await session.commit()

    response = client.post("/api/auth/logout")

    assert response.status_code == 401
    # Rejected, not deleted: a dependency that wrote would be committing on a
    # read path, against `get_session`'s contract.
    assert await sessions(session) == 1


async def test_logging_out_invalidates_the_token(client: TestClient, session: AsyncSession) -> None:
    sign_in(client)
    token = client.cookies[COOKIE_NAME]

    response = client.post("/api/auth/logout")

    assert response.status_code == 204
    assert await sessions(session) == 0
    # The browser is told to drop it; a client that keeps it anyway gets nothing.
    client.cookies.set(COOKIE_NAME, token)
    assert client.post("/api/auth/logout").status_code == 401


async def test_login_records_when_it_last_happened(
    client: TestClient, session: AsyncSession
) -> None:
    sign_in(client)

    user = await session.scalar(select(AppUser).order_by(AppUser.id).limit(1))
    assert user is not None
    assert user.last_login_at is not None


def test_every_endpoint_but_the_public_ones_needs_a_session(client: TestClient) -> None:
    """The guarantee itself, rather than a habit of remembering the dependency.

    Driven from the published contract and answered by the running app, not by
    reading the dependency tree: what protects an endpoint is that it returns
    401 without a cookie, and FastAPI is free to rearrange how routers hold
    their routes. Everything #10 and #11 add is guarded by default, because a
    router included without the dependency fails here rather than in production.
    """

    contract = client.get("/openapi.json").json()
    probed = {
        (method.upper(), path): client.request(
            method.upper(), re.sub(r"\{[^}]+\}", "1", path)
        ).status_code
        for path, operations in contract["paths"].items()
        for method in operations
    }
    # A contract that listed nothing would satisfy the assertion below vacuously.
    assert ("POST", "/api/auth/logout") in probed

    assert {route for route, code in probed.items() if code != 401} == PUBLIC


def test_no_response_schema_carries_the_password_hash(client: TestClient) -> None:
    """DoD, and cheap to keep true across every endpoint M1 still has to add."""

    contract = client.get("/openapi.json").text

    assert "password_hash" not in contract


async def test_the_first_start_creates_the_account_from_the_environment(
    client: TestClient, session: AsyncSession
) -> None:
    users = list(await session.scalars(select(AppUser)))

    assert [user.username for user in users] == [TEST_USERNAME]
    assert users[0].password_hash.startswith("$argon2id$")
    assert TEST_PASSWORD not in users[0].password_hash


async def test_a_changed_password_signs_everyone_out(
    client: TestClient, session: AsyncSession
) -> None:
    """The only lever an operator has before M5, so it has to be a real one.

    A rotation that leaves the stolen cookie working has rotated nothing.
    """

    sign_in(client)
    token = client.cookies[COOKIE_NAME]

    await bootstrap_user(session, username=TEST_USERNAME, password="a different one")

    assert await sessions(session) == 0
    client.cookies.set(COOKIE_NAME, token)
    assert client.post("/api/auth/logout").status_code == 401
    assert sign_in(client, password="a different one").status_code == 200


async def test_an_unchanged_password_keeps_the_sessions(
    client: TestClient, session: AsyncSession
) -> None:
    """Every restart runs the bootstrap, and a restart is not a rotation."""

    sign_in(client)

    await bootstrap_user(session, username=TEST_USERNAME, password=TEST_PASSWORD)

    assert await sessions(session) == 1
    assert client.post("/api/auth/logout").status_code == 204


async def test_a_changed_username_renames_the_one_account(
    client: TestClient, session: AsyncSession
) -> None:
    """Selected by id, not by name — otherwise a typo silently adds a second account."""

    await bootstrap_user(session, username="renamed", password=TEST_PASSWORD)

    users = list(await session.scalars(select(AppUser)))
    assert [user.username for user in users] == ["renamed"]
    assert sign_in(client, username="renamed").status_code == 200


async def test_the_bootstrap_is_what_creates_the_account(session: AsyncSession) -> None:
    """Without the app: the first call inserts, the second does not."""

    first = await bootstrap_user(session, username="solo", password=TEST_PASSWORD)
    second = await bootstrap_user(session, username="solo", password=TEST_PASSWORD)

    assert first.id == second.id
    assert await session.scalar(select(func.count()).select_from(AppUser)) == 1


def test_a_hash_we_cannot_parse_is_a_failed_login_not_a_crash() -> None:
    """A hand-edited row, or a restore from a database that predates argon2 here."""

    assert verify_password("not-a-hash", TEST_PASSWORD) is False


def test_the_same_password_hashes_differently_every_time() -> None:
    """Salted, which is the one property a homegrown mistake would lose."""

    assert hash_password(TEST_PASSWORD) != hash_password(TEST_PASSWORD)
    assert verify_password(hash_password(TEST_PASSWORD), TEST_PASSWORD) is True


@pytest.mark.parametrize("wrong", ["", " ", TEST_PASSWORD + " ", TEST_PASSWORD.upper()])
def test_close_is_not_close_enough(wrong: str) -> None:
    assert verify_password(hash_password(TEST_PASSWORD), wrong) is False


async def test_a_database_with_no_account_refuses_every_login(session: AsyncSession) -> None:
    """Unreachable through the app, since the bootstrap runs first. Not unreachable."""

    assert await authenticate(session, username=TEST_USERNAME, password=TEST_PASSWORD) is None
