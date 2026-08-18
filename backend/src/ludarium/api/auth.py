from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from ludarium.auth import (
    CurrentSession,
    authenticate,
    clear_session_cookie,
    open_session,
    set_session_cookie,
)
from ludarium.db import SessionDep
from ludarium.models.types import utcnow

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Bounded, because this is the one endpoint that answers before authenticating.

    Not because argon2's cost scales with the input — measured, a 10 MB password
    costs 62 ms against 49 ms for a short one, since the memory-hard phase is
    fixed. The reason is upstream of that: an unbounded string is buffered whole
    and carried through validation before anything gets to decide it is wrong.
    """

    username: str = Field(max_length=256)
    # `SecretStr` so that a validation error, a repr in a traceback or a stray
    # log line cannot carry it (rule 7).
    password: SecretStr = Field(max_length=1024)


class SessionResponse(BaseModel):
    """What the frontend needs to render a signed-in shell. The token is in the cookie."""

    username: str
    expires_at: datetime


@router.post("/login")
async def login(
    payload: LoginRequest, request: Request, response: Response, session: SessionDep
) -> SessionResponse:
    user = await authenticate(
        session, username=payload.username, password=payload.password.get_secret_value()
    )
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")

    record, token = await open_session(session, user=user)
    user.last_login_at = utcnow()
    await session.commit()

    set_session_cookie(response, request, token=token)
    return SessionResponse(username=user.username, expires_at=record.expires_at)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, session: SessionDep, record: CurrentSession) -> None:
    """Deleted, not flagged.

    Rule 1 is about the catalogue — a game that disappears is evidence, and a
    revoked session is a liability. There is nothing to audit here that
    `last_login_at` does not already say.
    """

    await session.delete(record)
    await session.commit()
    clear_session_cookie(response)
