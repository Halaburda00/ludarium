"""Running a sync on demand, and the status panel's one call.

Rule 4 is the shape of both: a request names one provider and touches only that
provider's accounts, and the status view reports each provider's health beside
its own last runs rather than as one number for the instance.
"""

from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.auth import CurrentSession
from ludarium.crypto import CredentialDecryptionError, get_cipher
from ludarium.db import SessionDep
from ludarium.enums import SyncStatus, SyncTrigger
from ludarium.models import Account, Provider, SyncRun
from ludarium.providers import LibraryProvider
from ludarium.providers.registry import build_library, supports
from ludarium.sync import SyncInProgressError, sync_account

RECENT_RUNS = 50

router = APIRouter(prefix="/sync", tags=["sync"])


class SyncRunResponse(BaseModel):
    id: int
    provider: str
    account_id: int | None
    trigger: SyncTrigger
    status: SyncStatus
    started_at: datetime
    finished_at: datetime | None
    items_seen: int
    items_added: int
    items_updated: int
    items_removed: int
    error_text: str | None


class ProviderStatusResponse(BaseModel):
    key: str
    display_name: str
    enabled: bool
    status: SyncStatus
    last_success_at: datetime | None
    last_error: str | None


class SyncOverviewResponse(BaseModel):
    """Both halves in one call, because the panel shows them together.

    A provider's health without its runs cannot say *what* failed, and the runs
    without the provider row cannot say whether it has ever worked.
    """

    providers: list[ProviderStatusResponse]
    runs: list[SyncRunResponse]


def _describe(run: SyncRun, provider_key: str) -> SyncRunResponse:
    return SyncRunResponse(
        id=run.id,
        provider=provider_key,
        account_id=run.account_id,
        trigger=run.trigger,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        items_seen=run.items_seen,
        items_added=run.items_added,
        items_updated=run.items_updated,
        items_removed=run.items_removed,
        error_text=run.error_text,
    )


async def _syncable(
    session: AsyncSession, key: str, user_id: int
) -> tuple[Provider, list[Account]]:
    provider = await session.scalar(select(Provider).where(Provider.key == key))
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no provider named `{key}`")
    if not supports(provider.key):
        # Before the accounts and before any credential: `manual` owns
        # entitlements and has nothing to ask, which is a fact about the
        # provider rather than about anything stored under it.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"`{key}` has no library client")
    accounts = list(
        await session.scalars(
            select(Account)
            .where(
                Account.provider_id == provider.id,
                Account.user_id == user_id,
                Account.is_active.is_(True),
                # Discovered inside an import, with no credentials of its own.
                Account.is_derived.is_(False),
            )
            .order_by(Account.id)
        )
    )
    if not accounts:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no `{key}` account is connected")
    return provider, accounts


@router.post("/{provider}")
async def run(
    provider: str, request: Request, session: SessionDep, record: CurrentSession
) -> list[SyncRunResponse]:
    """Sync every account of one provider and report each run.

    A list rather than one run because a platform may have several accounts
    (M4), and one of them failing is not the others' problem — `sync_account`
    turns a provider failure into a run status precisely so this loop does not
    have to catch anything to keep rule 4.

    `SyncInProgressError` is the exception, and it is the caller's answer rather
    than a run's: the double-click that produced it wants 409, not a second run.
    """

    reporter, accounts = await _syncable(session, provider, record.user_id)
    client: httpx.AsyncClient = request.app.state.http
    runs: list[SyncRun] = []
    for account in accounts:
        library = _library_for(account, reporter.key, client)
        try:
            runs.append(await sync_account(session, account=account, library=library))
        except SyncInProgressError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return [_describe(finished, reporter.key) for finished in runs]


def _library_for(account: Account, key: str, client: httpx.AsyncClient) -> LibraryProvider:
    if account.credentials_encrypted is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"account {account.id} has no stored credentials"
        )
    try:
        secret = get_cipher().decrypt(account.credentials_encrypted)
    except CredentialDecryptionError as exc:
        # The message names the key, never the ciphertext (rule 7).
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    # `_syncable` has already refused a provider with no client, so this cannot
    # raise `UnsupportedProviderError` here.
    return build_library(
        key,
        external_account_id=account.external_account_id or "",
        secret=secret,
        client=client,
    )


@router.get("/runs")
async def overview(session: SessionDep, record: CurrentSession) -> SyncOverviewResponse:
    providers = list(await session.scalars(select(Provider).order_by(Provider.key)))
    rows = await session.execute(
        select(SyncRun, Provider.key)
        .join(Provider, Provider.id == SyncRun.provider_id)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(RECENT_RUNS)
    )
    return SyncOverviewResponse(
        providers=[
            ProviderStatusResponse(
                key=provider.key,
                display_name=provider.display_name,
                enabled=provider.enabled,
                status=provider.status,
                last_success_at=provider.last_success_at,
                last_error=provider.last_error,
            )
            for provider in providers
        ],
        runs=[_describe(run, key) for run, key in rows],
    )
