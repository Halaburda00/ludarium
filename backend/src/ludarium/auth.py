"""One password from the environment, one cookie, and nothing that pretends to be more.

The instance holds a working Steam API key from the moment onboarding succeeds,
and the documented run command binds `0.0.0.0`. That is the whole reason this
exists: to close a door that is otherwise open to every device on the network.
It is not session management, and the columns say so — `user_agent`, a session
list and "sign out other devices" are M5, when someone other than the author
hosts this.

Single-tenant is ADR-0003: one row in `app_user`, no user management, and the
environment as the only place a credential comes from (rule 7).
"""

import asyncio
import hashlib
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from typing import Annotated, Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import SessionDep
from ludarium.models import AppUser, UserSession
from ludarium.models.types import utcnow

COOKIE_NAME: Final = "ludarium_session"
# Long, because the alternative is a self-hoster retyping a password on their
# phone every week. Shortening it is the operator's call once M5 gives them a
# session list to shorten it from.
SESSION_LIFETIME: Final = timedelta(days=30)
TOKEN_BYTES: Final = 32

# argon2-cffi's own defaults, deliberately not our own numbers: they are argon2id
# and they are revised by people who follow the parameter guidance, which we do
# not. `check_needs_rehash` below is what makes a later revision arrive.
_hasher = PasswordHasher()

# argon2 is expensive on purpose, and measured here it is 50-60ms of C that
# would otherwise run on the event loop — every concurrent request and every
# APScheduler job stalls behind each login attempt. The call releases the GIL,
# so a worker thread genuinely gets the loop back.
#
# Its own small pool rather than the default executor, because argon2 is
# expensive in memory as much as in time: each concurrent hash holds
# `memory_cost`, 64 MiB by default, so the ceiling on parallel hashing is the
# ceiling on what an unauthenticated endpoint can make us allocate. Two is
# generous for one account, and the rest wait.
_POOL: Final = ThreadPoolExecutor(max_workers=2, thread_name_prefix="argon2")


async def _off_the_loop[T](call: Callable[[], T]) -> T:
    return await asyncio.get_running_loop().run_in_executor(_POOL, call)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True or False, never an exception. A stored hash we cannot parse is a failed login.

    `InvalidHashError` is the case that matters: a hand-edited row, or a restore
    from a database that predates argon2 here. Letting it propagate would answer
    a wrong password with a 500 and tell the caller the difference.
    """

    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> str:
    """SHA-256, and on purpose not argon2.

    A password is short, human-chosen and worth an expensive hash. This token is
    256 bits out of `secrets`: there is nothing to guess, so the only job left is
    that a leaked database yields no usable cookie. A digest does that at index
    speed, where argon2 would put a 50ms verify on the front of every request.
    """

    return hashlib.sha256(token.encode()).hexdigest()


async def bootstrap_user(session: AsyncSession, *, username: str, password: str) -> AppUser:
    """The single account, reconciled with the environment on every start.

    Not "created if missing": the environment is the only lever the operator has
    (rule 7), and there is no password-change UI before M5. So a changed
    `LUDARIUM_PASSWORD` re-hashes here — and takes the existing sessions with it,
    because a password change that leaves the old cookies working is not a
    password change.

    Selected by id rather than by username, so that changing `LUDARIUM_USERNAME`
    renames the one account instead of quietly creating a second one that owns
    nothing.
    """

    user = await session.scalar(select(AppUser).order_by(AppUser.id).limit(1))
    if user is None:
        digest = await _off_the_loop(partial(hash_password, password))
        user = AppUser(username=username, password_hash=digest)
        session.add(user)
        await session.commit()
        return user

    changed = not await _off_the_loop(partial(verify_password, user.password_hash, password))
    if changed or _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = await _off_the_loop(partial(hash_password, password))
    if changed:
        await session.execute(delete(UserSession).where(UserSession.user_id == user.id))
    user.username = username
    await session.commit()
    return user


async def authenticate(session: AsyncSession, *, username: str, password: str) -> AppUser | None:
    """The single account, or None. One failure, never a reason.

    The password is verified even when the username is already wrong, so that
    the two mistakes cost the same time. With one account the username is barely
    a secret, but a login endpoint that answers faster for a wrong name is the
    shape of a bug, and the fix is one line.
    """

    user = await session.scalar(select(AppUser).order_by(AppUser.id).limit(1))
    if user is None:
        return None
    correct = await _off_the_loop(partial(verify_password, user.password_hash, password))
    if not correct or user.username != username:
        return None
    return user


async def open_session(session: AsyncSession, *, user: AppUser) -> tuple[UserSession, str]:
    """A new session row and the token that opens it. The token is returned, never stored."""

    token = new_token()
    record = UserSession(
        user_id=user.id,
        token_hash=token_digest(token),
        expires_at=utcnow() + SESSION_LIFETIME,
    )
    session.add(record)
    await session.flush()
    return record, token


def set_session_cookie(response: Response, request: Request, *, token: str) -> None:
    """httpOnly and SameSite=Lax, with `secure` taken from the scheme rather than assumed.

    Hardcoding `secure=True` would lock out the ordinary deployment — plain HTTP
    on a home network — and hardcoding it False would weaken the one behind a
    TLS proxy. Reading the request's scheme is right in both.

    Behind a reverse proxy that is only true with uvicorn's `--proxy-headers`;
    without it the scheme reads `http` and the flag is dropped. Login still
    works, which is why this is worth writing down: the symptom is a cookie that
    would travel over plain HTTP, not an error anyone would notice.
    """

    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, samesite="lax", path="/")


def _unauthenticated() -> HTTPException:
    # One answer for no cookie, an unknown cookie and an expired one. Which of
    # the three it was is a fact about our database, and the caller has not
    # earned it.
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")


async def current_session(
    session: SessionDep,
    token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> UserSession:
    """The session behind the cookie, or 401.

    An expired row is rejected and left alone: a dependency that deleted it
    would be writing on a read path, against `db.get_session`'s contract that
    the endpoint owns the commit. Sweeping them up is a scheduled job's work.
    """

    if token is None:
        raise _unauthenticated()
    record = await session.scalar(
        select(UserSession).where(UserSession.token_hash == token_digest(token))
    )
    if record is None or record.expires_at <= utcnow():
        raise _unauthenticated()
    return record


# The session, not the user: nothing needs the row yet, and `record.user_id` is
# what a user-scoped query in #10 will actually ask for.
CurrentSession = Annotated[UserSession, Depends(current_session)]
