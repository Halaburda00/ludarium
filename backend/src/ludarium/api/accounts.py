"""Connecting a platform account, which is the first thing a new instance does.

The credential is validated against the platform before anything is written, so
a wrong key is a 400 the user sees while they still have the page open, rather
than a failed background run they find later.
"""

from datetime import datetime
from typing import Annotated, Final, Self

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api.common import provider_or_404
from ludarium.auth import CurrentSession
from ludarium.crypto import get_cipher
from ludarium.db import SessionDep
from ludarium.enums import SyncErrorKind, SyncStatus
from ludarium.models import Account, Provider, SyncRun
from ludarium.models.types import utcnow
from ludarium.providers import (
    InvalidCredentialsError,
    LibraryNotVisibleError,
    ProviderError,
    RateLimitedError,
)
from ludarium.providers.registry import UnsupportedProviderError
from ludarium.providers.registry import connect as settle
from ludarium.sync import summarise

# Fixed, and deliberately not derived from the credential: a mask that mirrors
# the length of a secret is a fact about the secret (rule 7). The UI needs to
# know a key is stored, not how long it was.
MASK: Final = "••••••••"

router = APIRouter(prefix="/accounts", tags=["accounts"])


class ConnectRequest(BaseModel):
    provider: str = Field(max_length=64)
    # Public: the SteamID64 identifies the account and is half the unique key.
    # Empty for Epic, whose sign-in names the account itself.
    external_account_id: str = Field(default="", max_length=256)
    label: str = Field(default="Main", max_length=256)
    # Steam's Web API key, or the authorization code Epic's sign-in page showed.
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
    # What this account's last sync did, where the provider's status is the
    # worst across its accounts and cannot say which one (#26).
    status: SyncStatus
    last_error: str | None
    # Who can fix the last failure: `credentials` is the user's, by signing in
    # again. From the account's latest finished run; null when it succeeded.
    error_kind: SyncErrorKind | None
    credentials: str | None
    provider_name: str
    # Made by an import rather than connected: no credential, and synced only by
    # whatever reported it (ADR-0035).
    is_derived: bool


class AccountUpdate(BaseModel):
    """The two things a user decides about an account. A field left out is left alone."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
    ] = "Main"
    # Off stops every sync of it, by hand or on the schedule. Its games stay:
    # switching an account off is not removing anything (rule 1).
    is_active: bool = True

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        return self


def _describe(
    account: Account, provider: Provider, error_kind: SyncErrorKind | None = None
) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        provider=provider.key,
        external_account_id=account.external_account_id,
        label=account.label,
        is_active=account.is_active,
        created_at=account.created_at,
        last_success_at=account.last_success_at,
        status=account.status,
        last_error=account.last_error,
        error_kind=error_kind,
        credentials=MASK if account.credentials_encrypted else None,
        provider_name=provider.display_name,
        is_derived=account.is_derived,
    )


async def _error_kinds(session: AsyncSession, account_ids: list[int]) -> dict[int, SyncErrorKind]:
    """The error kind of each account's latest finished run, where it failed."""

    latest = (
        select(func.max(SyncRun.id))
        .where(SyncRun.account_id.in_(account_ids), SyncRun.status != SyncStatus.RUNNING)
        .group_by(SyncRun.account_id)
    )
    rows = await session.execute(
        select(SyncRun.account_id, SyncRun.error_kind).where(SyncRun.id.in_(latest))
    )
    return {
        account_id: kind for account_id, kind in rows if account_id is not None and kind is not None
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def connect(
    payload: ConnectRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    record: CurrentSession,
) -> AccountResponse:
    """Validate first, then store. A rejected key leaves nothing behind.

    An account already connected is connected again rather than refused: its
    credential is replaced and it is active once more, answered 200. That is
    how an Epic account whose sign-in ended comes back, and how a Steam key is
    rotated, without losing the account's history.

    The four provider errors are three different answers, because they are three
    different jobs: a wrong key and a private profile are the user's to fix and
    say so now, while an outage is nobody's fault and must not be reported as a
    bad credential — told that, someone rotates a key that was working.
    """

    provider = await provider_or_404(session, payload.provider)
    client: httpx.AsyncClient = request.app.state.http
    secret = payload.credentials.get_secret_value()
    try:
        connected = await settle(
            provider.key,
            external_account_id=payload.external_account_id,
            credential=secret,
            client=client,
        )
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

    existing = await session.scalar(
        select(Account).where(
            Account.provider_id == provider.id,
            Account.external_account_id == connected.external_account_id,
        )
    )
    if existing is not None and existing.user_id != record.user_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"`{payload.provider}` account {connected.external_account_id} is already connected",
        )
    if existing is not None:
        existing.credentials_encrypted = get_cipher().encrypt(connected.secret)
        existing.credentials_updated_at = utcnow()
        existing.is_active = True
        # An account a report made is the user's once they connect it: its
        # platform syncs it from now on, and reports about it are refused
        # (ADR-0035). The copies the report brought are kept and upserted.
        existing.is_derived = False
        await session.commit()
        response.status_code = status.HTTP_200_OK
        return _describe(existing, provider)

    account = Account(
        user_id=record.user_id,
        provider_id=provider.id,
        external_account_id=connected.external_account_id,
        label=payload.label,
        credentials_encrypted=get_cipher().encrypt(connected.secret),
        credentials_updated_at=utcnow(),
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"`{payload.provider}` account {connected.external_account_id} is already connected",
        ) from exc
    return _describe(account, provider)


@router.get("")
async def connected(session: SessionDep, record: CurrentSession) -> list[AccountResponse]:
    rows = list(
        await session.execute(
            select(Account, Provider)
            .join(Provider, Provider.id == Account.provider_id)
            .where(Account.user_id == record.user_id)
            .order_by(Account.id)
        )
    )
    kinds = await _error_kinds(session, [account.id for account, _ in rows])
    return [_describe(account, provider, kinds.get(account.id)) for account, provider in rows]


@router.patch("/{account_id}")
async def update(
    account_id: int, payload: AccountUpdate, session: SessionDep, record: CurrentSession
) -> AccountResponse:
    """Rename an account, or switch it off or on. Nothing about its games changes."""

    account = await session.get(Account, account_id)
    if account is None or account.user_id != record.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such account")
    changes = payload.model_dump(include=payload.model_fields_set)
    for field, value in changes.items():
        setattr(account, field, value)
    if "is_active" in changes:
        # Every provider that has reported for it: its own platform's, and an
        # importer's for a derived account. Their health counts active
        # accounts only, so switching one changes what they report.
        reporters = await session.scalars(
            select(Provider).where(
                Provider.id.in_(select(SyncRun.provider_id).where(SyncRun.account_id == account.id))
            )
        )
        for reporter in reporters:
            await summarise(session, reporter)
    await session.commit()
    provider = await session.get_one(Provider, account.provider_id)
    kinds = await _error_kinds(session, [account.id])
    return _describe(account, provider, kinds.get(account.id))
