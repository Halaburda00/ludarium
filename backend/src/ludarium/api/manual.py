"""Games the user adds by hand: a disc, an unredeemed key, an itch.io download (#97).

No platform reports these, so no sync touches them (rule 2) and the user is
their only writer. Where each field the user types is stored, and why deleting
one is a `DELETE`, is ADR-0031.
"""

from typing import Annotated, Final

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.enums import (
    EntitlementOrigin,
    EntityType,
    ItemKind,
    OwnershipType,
    SourceKind,
    WorkLinkRole,
)
from ludarium.filters import FIRST_YEAR, LAST_YEAR
from ludarium.merging import delete_works
from ludarium.models import Account, Entitlement, EntitlementWork, FieldProvenance, Provider, Work
from ludarium.models.types import ScalarValue
from ludarium.resolver import record_many, resolve
from ludarium.sync import create_stubs
from ludarium.titles import sort_title

# The provider row, and the `source_ref` of every provenance row the user writes
# here: a provider key, as every other source's is.
MANUAL: Final = "manual"
# The account every manual copy of a user belongs to.
ACCOUNT_LABEL: Final = "Manual entry"
# Longer than any title a store has printed.
MAX_TITLE: Final = 300
# A few words: "PS5 disc", "Humble key".
MAX_LABEL: Final = 100
# The work's fields the user types, as `manual` provenance (ADR-0031).
WORK_FIELDS: Final = ("item_kind", "release_year")

router = APIRouter(prefix="/entitlements/manual", tags=["entitlements"])

type Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE)
]


class ManualEntry(BaseModel):
    """A copy no platform knows about, as the user describes it.

    The whole entry on every write, so the form that edits one sends what it
    shows: a field left out is its default, never "unchanged".
    """

    model_config = ConfigDict(extra="forbid")

    title: Title
    # Where it lives. Blank is none, so the form has one empty state.
    store_label: str | None = Field(default=None, max_length=MAX_LABEL)
    ownership_type: OwnershipType = OwnershipType.OWNED
    # Asked for, because the kind filter matches nothing to a work with no
    # kind, and a manual entry would leave the `game` filter (ADR-0031).
    item_kind: ItemKind = ItemKind.GAME
    release_year: int | None = Field(default=None, ge=FIRST_YEAR, le=LAST_YEAR)

    @field_validator("store_label")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class ManualEntryResponse(BaseModel):
    """The entry as the user last wrote it, and the work it is a copy of."""

    id: int
    work_id: int
    title: str
    store_label: str | None
    ownership_type: OwnershipType
    # What the user said, not the resolved values: a matched work may show
    # IGDB's year, and a form that saved that back would make it the user's.
    # Null where they said nothing, which for a kind is an entry made outside
    # this API.
    item_kind: ItemKind | None
    release_year: int | None


@router.post("", status_code=status.HTTP_201_CREATED)
async def add(
    entry: ManualEntry, session: SessionDep, record: CurrentSession
) -> ManualEntryResponse:
    """A manual copy on the user's `manual` account, and the stub it is a copy of (ADR-0015)."""

    account = await _account(session, record.user_id)
    entitlement = Entitlement(
        user_id=record.user_id,
        account_id=account.id,
        origin=EntitlementOrigin.MANUAL,
        # The column is NOT NULL and the provenance row needs the entitlement's
        # id; the resolver writes it again a statement later.
        provider_title=entry.title,
    )
    session.add(entitlement)
    await session.flush()
    await create_stubs(session, user_id=record.user_id, entitlements=[entitlement], run_id=None)
    await session.flush()
    response = await _write(session, entitlement, entry, record.user_id)
    await session.commit()
    return response


@router.get("/{entitlement_id}")
async def read(
    entitlement_id: int, session: SessionDep, record: CurrentSession
) -> ManualEntryResponse:
    entitlement = await _manual(session, entitlement_id, record.user_id)
    work_id = await _primary(session, entitlement.id)
    if work_id is None:
        # Every write path gives an entry a work, and a merge moves the link
        # rather than dropping it. A row without one was made somewhere else.
        raise HTTPException(status.HTTP_409_CONFLICT, "the entry is not a copy of any work")
    return await _describe(session, entitlement, work_id)


@router.put("/{entitlement_id}")
async def replace(
    entitlement_id: int, entry: ManualEntry, session: SessionDep, record: CurrentSession
) -> ManualEntryResponse:
    entitlement = await _manual(session, entitlement_id, record.user_id)
    response = await _write(session, entitlement, entry, record.user_id)
    await session.commit()
    return response


@router.delete("/{entitlement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove(entitlement_id: int, session: SessionDep, record: CurrentSession) -> None:
    """Delete the entry, and its stub when nothing else would miss it (ADR-0031).

    A `DELETE` rather than a removal: rule 1 guards against a platform's silence,
    and this is the user's own word about a row they typed in.
    """

    entitlement = await _manual(session, entitlement_id, record.user_id)
    work_ids = list(
        await session.scalars(
            select(EntitlementWork.work_id).where(EntitlementWork.entitlement_id == entitlement.id)
        )
    )
    # Polymorphic, so no foreign key cascades these away.
    await session.execute(
        delete(FieldProvenance).where(
            FieldProvenance.entity_type == EntityType.ENTITLEMENT,
            FieldProvenance.entity_id == entitlement.id,
        )
    )
    # Its links go with it, by `ON DELETE CASCADE`.
    await session.execute(delete(Entitlement).where(Entitlement.id == entitlement.id))
    child = aliased(Work)
    unreached = list(
        await session.scalars(
            select(Work.id).where(
                Work.id.in_(work_ids),
                Work.is_matched.is_(False),
                ~exists().where(EntitlementWork.work_id == Work.id),
                # A parent is kept: `ON DELETE RESTRICT` would refuse it anyway.
                ~exists().where(child.parent_work_id == Work.id),
            )
        )
    )
    await delete_works(session, unreached)
    for work_id in set(work_ids) - set(unreached):
        await _forget(session, work_id)
    await session.commit()


async def _account(session: AsyncSession, user_id: int) -> Account:
    """The user's `manual` account, made with their first entry.

    Check-then-act inside the request's transaction, which on SQLite is `BEGIN
    IMMEDIATE`: a second request reads after the first has committed and finds
    the account it made (ADR-0017).
    """

    provider = await session.scalar(select(Provider).where(Provider.key == MANUAL))
    if provider is None:
        # Seeded on every start, so this is a database nobody started the app on.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "no `manual` provider")
    account = await session.scalar(
        select(Account)
        .where(Account.user_id == user_id, Account.provider_id == provider.id)
        .order_by(Account.id)
        .limit(1)
    )
    if account is not None:
        return account
    account = Account(user_id=user_id, provider_id=provider.id, label=ACCOUNT_LABEL)
    session.add(account)
    await session.flush()
    return account


async def _manual(session: AsyncSession, entitlement_id: int, user_id: int) -> Entitlement:
    """A manual copy of this user's, or 404.

    A synced copy is a 404 here too, not a 403: this API has nothing to say
    about it, and rule 2's other half is that the user does not edit what the
    platform reports through the door meant for what it does not.
    """

    entitlement = await session.get(Entitlement, entitlement_id)
    if (
        entitlement is None
        or entitlement.user_id != user_id
        or entitlement.origin is not EntitlementOrigin.MANUAL
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such manual entry")
    return entitlement


async def _primary(session: AsyncSession, entitlement_id: int) -> int | None:
    work_id: int | None = await session.scalar(
        select(EntitlementWork.work_id).where(
            EntitlementWork.entitlement_id == entitlement_id,
            EntitlementWork.role == WorkLinkRole.PRIMARY,
        )
    )
    return work_id


async def _write(
    session: AsyncSession, entitlement: Entitlement, entry: ManualEntry, user_id: int
) -> ManualEntryResponse:
    """Everything the entry says, each field where ADR-0031 puts it. Does not commit."""

    entitlement.ownership_type = entry.ownership_type
    entitlement.store_label = entry.store_label
    recorded = await record_many(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        source_kind=SourceKind.MANUAL,
        source_ref=MANUAL,
        values={"provider_title": entry.title},
    )
    await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=["provider_title"],
        recorded=recorded,
    )

    work_id = await _primary(session, entitlement.id)
    if work_id is None:
        # No state in which an entitlement has no work (ADR-0015).
        [stub] = await create_stubs(
            session, user_id=user_id, entitlements=[entitlement], run_id=None
        )
        await session.flush()
        work_id = stub.id
    work = await session.get_one(Work, work_id)
    if not work.is_matched:
        # A stub's name is the copy's, as sync gives it (ADR-0015). A matched
        # work's is IGDB's, and the user's name for the disc does not replace it.
        work.title = entry.title
        work.sort_title = sort_title(entry.title)

    said: dict[str, ScalarValue | None] = {"item_kind": entry.item_kind.value}
    if entry.release_year is not None:
        said["release_year"] = entry.release_year
    recorded = await record_many(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        source_kind=SourceKind.MANUAL,
        source_ref=MANUAL,
        values=said,
    )
    await resolve(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        fields=list(said),
        recorded=recorded,
    )
    if entry.release_year is None:
        await _withdraw(session, work, "release_year")
    return await _describe(session, entitlement, work.id)


async def _forget(session: AsyncSession, work_id: int) -> None:
    """Take back what a deleted entry said about a work that stays.

    Left behind, its year and kind would outrank IGDB for good with no form left
    to change them. Kept while another manual copy reaches the work: the rows
    are keyed by work and field, not by entry, so they are that copy's too.
    """

    shared = await session.scalar(
        select(
            exists().where(
                EntitlementWork.work_id == work_id,
                EntitlementWork.entitlement_id == Entitlement.id,
                Entitlement.origin == EntitlementOrigin.MANUAL,
            )
        )
    )
    if shared:
        return
    work = await session.get_one(Work, work_id)
    for field in WORK_FIELDS:
        await _withdraw(session, work, field)


async def _withdraw(session: AsyncSession, work: Work, field: str) -> None:
    """Take back what the user said, so whatever else is known shows through.

    Deleting the row rather than recording a null: a null from `manual` would
    outrank IGDB's year and blank it, and "I did not give a year" is not "it
    has none".
    """

    await session.execute(
        delete(FieldProvenance).where(
            FieldProvenance.entity_type == EntityType.WORK,
            FieldProvenance.entity_id == work.id,
            FieldProvenance.field == field,
            FieldProvenance.source_kind == SourceKind.MANUAL,
            FieldProvenance.source_ref == MANUAL,
        )
    )
    written = await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=[field])
    if field not in written:
        # Nobody asserts it any more, and `resolve` leaves an unasserted field
        # alone. The column is a cache of the decision, and there is none.
        setattr(work, field, None)


async def _describe(
    session: AsyncSession, entitlement: Entitlement, work_id: int
) -> ManualEntryResponse:
    said = {
        row.field: row.value
        for row in await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == EntityType.WORK,
                FieldProvenance.entity_id == work_id,
                FieldProvenance.field.in_(WORK_FIELDS),
                FieldProvenance.source_kind == SourceKind.MANUAL,
                FieldProvenance.source_ref == MANUAL,
            )
        )
    }
    kind, year = said.get("item_kind"), said.get("release_year")
    return ManualEntryResponse(
        id=entitlement.id,
        work_id=work_id,
        title=entitlement.provider_title,
        store_label=entitlement.store_label,
        ownership_type=entitlement.ownership_type,
        item_kind=ItemKind(kind) if isinstance(kind, str) else None,
        release_year=year if isinstance(year, int) else None,
    )
