"""Copies the platform stopped listing, and giving them back in one click (rule 1, #96).

A removal is never a deletion, so everything a restore needs is still on the
row. What a restore adds is the user's word: the copy is kept, and later runs
leave it alone while the platform stays silent about it (ADR-0030).
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ludarium.api.works import state_row
from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.enums import WorkLinkRole
from ludarium.models import Account, Entitlement, EntitlementWork, Provider, Work
from ludarium.models.types import utcnow
from ludarium.queries import removed_from
from ludarium.resolver import resolve_work_aggregates_many

router = APIRouter(prefix="/entitlements", tags=["entitlements"])


class RemovedEntitlement(BaseModel):
    """A copy a successful run no longer saw, and when."""

    id: int
    provider: str
    provider_name: str
    account_label: str
    provider_title: str
    # The work it is the primary route to. Null only for a row whose link a
    # merge left behind, which the view still offers back.
    work_id: int | None
    work_title: str | None
    removed_at: datetime
    removed_by_run_id: int | None


@router.get("/removed")
async def removed(session: SessionDep, record: CurrentSession) -> list[RemovedEntitlement]:
    """Every removed copy, the most recently removed first.

    A manual copy is never swept (rule 2), so it never appears here.
    """

    rows = await session.execute(
        select(Entitlement, Account.label, Provider, Work.id, Work.title)
        .join(Account, Account.id == Entitlement.account_id)
        .join(Provider, Provider.id == Account.provider_id)
        .outerjoin(
            EntitlementWork,
            (EntitlementWork.entitlement_id == Entitlement.id)
            & (EntitlementWork.role == WorkLinkRole.PRIMARY),
        )
        .outerjoin(Work, Work.id == EntitlementWork.work_id)
        .where(*removed_from(record.user_id))
    )
    listed: list[RemovedEntitlement] = []
    for entitlement, label, provider, work_id, work_title in rows:
        # Not null by the predicate above; mypy cannot see that.
        assert entitlement.removed_at is not None
        listed.append(
            RemovedEntitlement(
                id=entitlement.id,
                provider=provider.key,
                provider_name=provider.display_name,
                account_label=label,
                provider_title=entitlement.provider_title,
                work_id=work_id,
                work_title=work_title,
                removed_at=entitlement.removed_at,
                removed_by_run_id=entitlement.removed_by_run_id,
            )
        )
    # Ordered here rather than in SQL: a removed view is a handful of rows, and
    # the predicate's columns stay in `queries`.
    listed.sort(key=lambda copy: (-copy.removed_at.timestamp(), copy.id))
    return listed


@router.post("/{entitlement_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore(entitlement_id: int, session: SessionDep, record: CurrentSession) -> None:
    """Put a removed copy back, and keep it there while the platform stays silent (ADR-0030).

    Its works count it again: a removed copy stops adding to `user_work_state`,
    and a work whose only copy this was comes back to the library with the
    state the user left on it, which a removal never touched.

    409 for a copy that is not removed: there is nothing to undo, and a kept
    flag set on a live copy would be a decision the user did not make.
    """

    entitlement = await _owned(session, entitlement_id, record.user_id)
    if entitlement.removed_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the copy is not removed")
    entitlement.removed_at = None
    entitlement.removed_by_run_id = None
    entitlement.kept_at = utcnow()
    work_ids = list(
        await session.scalars(
            select(EntitlementWork.work_id).where(EntitlementWork.entitlement_id == entitlement.id)
        )
    )
    for work_id in work_ids:
        await state_row(session, user_id=record.user_id, work_id=work_id)
    await session.flush()
    await resolve_work_aggregates_many(session, work_ids=work_ids, user_id=record.user_id)
    await session.commit()


@router.delete("/{entitlement_id}/keep", status_code=status.HTTP_204_NO_CONTENT)
async def let_go(entitlement_id: int, session: SessionDep, record: CurrentSession) -> None:
    """Stop keeping a restored copy: the next successful run that does not list it removes it.

    Nothing is removed here. The platform decides that, as it does for any
    other copy; this only withdraws the user's word against it.
    """

    entitlement = await _owned(session, entitlement_id, record.user_id)
    entitlement.kept_at = None
    await session.commit()


async def _owned(session: SessionDep, entitlement_id: int, user_id: int) -> Entitlement:
    entitlement = await session.get(Entitlement, entitlement_id)
    if entitlement is None or entitlement.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such copy")
    return entitlement
