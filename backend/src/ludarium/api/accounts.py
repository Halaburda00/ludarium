"""Connecting a platform account, which is the first thing a new instance does.

The credential is validated against the platform before anything is written, so
a wrong key is a 400 the user sees while they still have the page open, rather
than a failed background run they find later.
"""

from datetime import datetime
from typing import Final

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ludarium.api.common import provider_or_404
from ludarium.auth import CurrentSession
from ludarium.crypto import get_cipher
from ludarium.db import SessionDep
from ludarium.models import Account, Provider
from ludarium.models.types import utcnow
from ludarium.providers import (
    InvalidCredentialsError,
    LibraryNotVisibleError,
    ProviderError,
    RateLimitedError,
)
from ludarium.providers.registry import UnsupportedProviderError, build_library

# Fixed, and deliberately not derived from the credential: a mask that mirrors
# the length of a secret is a fact about the secret (rule 7). The UI needs to
# know a key is stored, not how long it was.
MASK: Final = "••••••••"

router = APIRouter(prefix="/accounts", tags=["accounts"])


class ConnectRequest(BaseModel):
    provider: str = Field(max_length=64)
    # Public: the SteamID64 identifies the account and is half the unique key.
    external_account_id: str = Field(max_length=256)
    label: str = Field(default="Main", max_length=256)
    # `SecretStr` so no validation error, traceback repr or log line carries it.
    credentials: SecretStr = Field(max_length=1024)


class AccountResponse(BaseModel):
    """What the frontend may know about a connected account.

    `credentials` is the mask and nothing else. There is no field here that
    could carry the ciphertext either: an encrypted secret in a response is
    still the secret, one key away.
    """

    id: int
    provider: str
    external_account_id: str | None
    label: str
    is_active: bool
    created_at: datetime
    last_success_at: datetime | None
    credentials: str | None


def _describe(account: Account, provider_key: str) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        provider=provider_key,
        external_account_id=account.external_account_id,
        label=account.label,
        is_active=account.is_active,
        created_at=account.created_at,
        last_success_at=account.last_success_at,
        credentials=MASK if account.credentials_encrypted else None,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def connect(
    payload: ConnectRequest, request: Request, session: SessionDep, record: CurrentSession
) -> AccountResponse:
    """Validate first, then store. A rejected key leaves nothing behind.

    The four provider errors are three different answers, because they are three
    different jobs: a wrong key and a private profile are the user's to fix and
    say so now, while an outage is nobody's fault and must not be reported as a
    bad credential — told that, someone rotates a key that was working.
    """

    provider = await provider_or_404(session, payload.provider)
    client: httpx.AsyncClient = request.app.state.http
    secret = payload.credentials.get_secret_value()
    try:
        library = build_library(
            provider.key,
            external_account_id=payload.external_account_id,
            secret=secret,
            client=client,
        )
        await library.validate_credentials()
    except UnsupportedProviderError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (InvalidCredentialsError, LibraryNotVisibleError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except RateLimitedError as exc:
        # Steam's own figure, passed straight through. Without it the frontend
        # has nothing to base a retry on but a guess, and a guessed retry into a
        # rate limit is how it becomes a ban.
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc), headers=headers) from exc
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    account = Account(
        user_id=record.user_id,
        provider_id=provider.id,
        external_account_id=payload.external_account_id,
        label=payload.label,
        credentials_encrypted=get_cipher().encrypt(secret),
        credentials_updated_at=utcnow(),
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"`{payload.provider}` account {payload.external_account_id} is already connected",
        ) from exc
    return _describe(account, provider.key)


@router.get("")
async def connected(session: SessionDep, record: CurrentSession) -> list[AccountResponse]:
    rows = await session.execute(
        select(Account, Provider.key)
        .join(Provider, Provider.id == Account.provider_id)
        .where(Account.user_id == record.user_id)
        .order_by(Account.id)
    )
    return [_describe(account, key) for account, key in rows]
