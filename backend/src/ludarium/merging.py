"""Folding one work into another, undoing it, and collecting stubs nothing reaches (#48).

`merge_work` is the one matcher action that destroys a row, so it records the
row and everything it took with it on a `match_audit` row, and `undo_merge`
rebuilds the source from that record (rule 6). The table in `docs/schema.md`,
"Merging stubs", is the specification; tables it names that do not exist yet —
`field_pin`, the platform links, embeddings, aliases and the
review queue — are left for the change that creates them to add here.

The undo restores the source under a new id. SQLite reuses the highest rowid
once it is deleted, and the source of a merge is usually the youngest work, so
its old id may already belong to someone else by the time anyone asks.

A row that changed hands after the merge is left where it now is: every undo
statement names the work the merge put the row on, and matches nothing if a
later operation moved it on.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Final

from sqlalchemy import ColumnElement, delete, exists, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from ludarium.enums import (
    EntityType,
    MatchAction,
    MatchActor,
    MatchLayer,
    PlayStatus,
    SourceKind,
    WorkLinkRole,
)
from ludarium.models import (
    Base,
    Edition,
    Entitlement,
    EntitlementWork,
    ExternalId,
    FieldProvenance,
    ImageAsset,
    MatchAudit,
    UserWorkState,
    Work,
    WorkCompany,
    WorkGenre,
)
from ludarium.queries import in_batches
from ludarium.resolver import resolve, resolve_work_aggregates_many

logger = logging.getLogger(__name__)

type Reference = InstrumentedAttribute[int] | InstrumentedAttribute[int | None]

# What `user_work_state` holds before the user has touched it. A merge fills
# only these from the source, and the orphan job keeps any stub whose state
# differs from them. `playtime_minutes` and `platform_count` are absent on
# purpose: they are aggregates, recomputed rather than carried.
USER_DEFAULTS: Final[Mapping[str, object]] = {
    "play_status": PlayStatus.NOT_STARTED,
    "rating": None,
    "notes": None,
    "is_favourite": False,
    "is_hidden": False,
    "started_at": None,
    "completed_at": None,
}

NO_IMAGES: Final[Mapping[str, list[Any]]] = {"moved": [], "dropped": []}
# The same, for a payload written before companies were.
NO_COMPANIES: Final[Mapping[str, list[Any]]] = {"moved": [], "dropped": []}
NO_LINKS: Final[Mapping[str, list[Any]]] = {"moved": [], "dropped": []}

# Bumped when the shape of `details` changes, so an undo can refuse a payload
# it would misread instead of half-restoring it.
PAYLOAD_VERSION: Final = 1


class MergeError(Exception):
    """A merge or an undo that cannot be carried out as asked."""


class MergeConflictError(MergeError):
    """Both works carry a `single_source` assertion for one field, from different sources.

    Not a tie to break: the registry says one source may assert the field, and
    two works each had one. Folding them would leave a row nothing may resolve,
    so the pair belongs in the review queue rather than in an `IntegrityError`.
    """


async def merge_work(
    session: AsyncSession,
    *,
    source_id: int,
    target_id: int,
    layer: MatchLayer | None,
    actor: MatchActor,
) -> MatchAudit:
    """Fold `source` into `target`, delete `source`, and return the audit row that undoes it.

    The target is the anchored work where there is one, which the caller
    decides and this enforces: an anchored source would take its IGDB id into a
    work whose `igdb_id` copy says something else (ADR-0021).

    Every check runs before the first write, so a refused merge leaves nothing
    to roll back. Does not commit.
    """

    if source_id == target_id:
        raise MergeError(f"work {source_id} cannot be merged into itself")
    source = await session.get(Work, source_id)
    target = await session.get(Work, target_id)
    if source is None or target is None:
        raise MergeError(f"no work {source_id if source is None else target_id}")
    if source.is_matched:
        raise MergeError(
            f"work {source_id} is anchored to IGDB; it can be a merge's target, not its source"
        )
    await _check_sole_source(session, source_id, target_id)

    details: dict[str, Any] = {
        "version": PAYLOAD_VERSION,
        "source": _snapshot(source),
        "target_parent_work_id": target.parent_work_id,
    }
    details["children"] = await _adopt_children(session, source, target)
    # The rows behind those columns, or the next resolve puts the deleted id back.
    details["claims"] = {
        "children": await _repoint_claims(session, details["children"], source_id, target_id),
        "target": await _repoint_claims(session, [target_id], source_id, None),
    }
    details["editions"] = await _fold_editions(session, source_id, target_id)
    details["links"] = await _move_links(session, source_id, target_id)
    details["provenance"] = await _fold_provenance(session, EntityType.WORK, source_id, target_id)
    # What the resolver is about to overwrite. A field the source brought the
    # only rows for is not resolved again by an undo — nothing asserts it on the
    # target any more — so the column has to be put back by hand.
    details["target_columns"] = _columns(target, details["provenance"]["fields"])
    details["external_ids"] = await _repoint(
        session,
        ExternalId.id,
        ExternalId.entity_id,
        source_id,
        target_id,
        ExternalId.entity_type == EntityType.WORK,
    )
    details["states"] = await _merge_states(session, source_id, target_id)
    details["images"] = await _move_images(session, source_id, target_id)
    details["companies"] = await _move_companies(session, source_id, target_id)
    details["genres"] = await _move_genres(session, source_id, target_id)
    details["audits"] = await _repoint(
        session, MatchAudit.id, MatchAudit.work_id, source_id, target_id
    )

    await session.delete(source)
    await session.flush()

    await _resolve_all(session, EntityType.WORK, target_id)
    await _aggregate(session, [target_id])

    audit = MatchAudit(
        work_id=target_id,
        action=MatchAction.MERGED,
        layer=layer,
        previous_work_id=source_id,
        details=details,
        actor=actor,
    )
    session.add(audit)
    await session.flush()
    return audit


async def undo_merge(session: AsyncSession, *, audit_id: int, actor: MatchActor) -> MatchAudit:
    """Rebuild the work a `merged` row deleted, and return the `unmerged` row recording it.

    The restored work is the new row's `work_id`. Does not commit.
    """

    merged = await session.get(MatchAudit, audit_id)
    if merged is None or merged.action is not MatchAction.MERGED or merged.details is None:
        raise MergeError(f"match_audit {audit_id} is not a merge")
    details = merged.details
    if details.get("version") != PAYLOAD_VERSION:
        raise MergeError(f"match_audit {audit_id} holds a payload this code cannot read")
    if await _undone(session, merged):
        raise MergeError(f"match_audit {audit_id} has already been undone")
    later = await _folded_since(session, merged)
    if later is not None:
        raise MergeError(
            f"work {merged.work_id} was merged since, by match_audit {later.id}; undo that first"
        )
    target_id = merged.work_id
    target = await session.get(Work, target_id)
    if target is None:
        raise MergeError(f"work {target_id} no longer exists; undo whatever removed it first")

    source_snapshot = dict(details["source"])
    old_id = source_snapshot["id"]
    parent = source_snapshot.get("parent_work_id")
    if parent is not None and await session.get(Work, parent) is None:
        source_snapshot["parent_work_id"] = None
    work = _restore(Work, source_snapshot)
    session.add(work)
    await session.flush()
    ids = {old_id: work.id}

    # The target's parent may have been the source, which is back under a new id.
    target.parent_work_id = ids.get(
        details["target_parent_work_id"], details["target_parent_work_id"]
    )
    await _repoint_back(
        session, Work.id, Work.parent_work_id, details["children"], target_id, work.id
    )
    # A payload written before parents were claimed (#98) has none to put back.
    claims = details.get("claims", {"children": [], "target": []})
    await _restore_claims(session, claims["children"], target_id, work.id)
    await _restore_claims(session, claims["target"], None, work.id)
    await _unfold_editions(session, details["editions"], target_id, work.id)
    await _unmove_links(session, details["links"], target_id, work.id)
    await _unfold_provenance(session, EntityType.WORK, details["provenance"], target_id, work.id)
    _put_back(target, details["target_columns"])
    await _repoint_back(
        session, ExternalId.id, ExternalId.entity_id, details["external_ids"], target_id, work.id
    )
    await _unmerge_states(session, details["states"], target_id, work.id)
    # A payload written before images were moved has none to put back.
    await _unmove_images(session, details.get("images", NO_IMAGES), target_id, work.id)
    await _unmove_companies(session, details.get("companies", NO_COMPANIES), target_id, work.id)
    # A payload written before genres existed (#92) has none to put back.
    await _unmove_genres(session, details.get("genres", NO_LINKS), target_id, work.id)
    await _repoint_back(
        session, MatchAudit.id, MatchAudit.work_id, details["audits"], target_id, work.id
    )
    await session.flush()

    for work_id in (target_id, work.id):
        await _resolve_all(session, EntityType.WORK, work_id)
    await _aggregate(session, [target_id, work.id])

    unmerged = MatchAudit(
        work_id=work.id,
        action=MatchAction.UNMERGED,
        layer=merged.layer,
        previous_work_id=target_id,
        details={"undoes": merged.id},
        actor=actor,
    )
    session.add(unmerged)
    await session.flush()
    return unmerged


@dataclass(frozen=True, slots=True)
class OrphanReport:
    deleted: list[int]
    # Unreached, but matched or carrying something the user wrote. Surfaced
    # rather than dropped: deleting them would lose the user's work.
    kept: list[int]


async def collect_orphan_stubs(session: AsyncSession) -> OrphanReport:
    """Delete the works no entitlement reaches that nobody would miss.

    A stub is kept if it is matched, has children, carries user state beyond
    the defaults, or has a `manual` provenance row on itself or an edition.

    Only works with no `entitlement_work` row at all. A removed entitlement
    keeps its link (rule 1), so the stub behind it stays and a restore finds it
    where it was; this job deletes works, never entitlements. Does not commit.
    """

    unreached = list(
        await session.scalars(
            select(Work.id)
            .where(~exists().where(EntitlementWork.work_id == Work.id))
            .order_by(Work.id)
        )
    )
    deleted: list[int] = []
    kept: list[int] = []
    for work_id in unreached:
        if await _worth_keeping(session, work_id):
            kept.append(work_id)
        else:
            deleted.append(work_id)

    await delete_works(session, deleted)

    if kept:
        logger.info(
            "%d works have no entitlement but are kept: matched, a parent, or edited by the user",
            len(kept),
        )
    return OrphanReport(deleted=deleted, kept=kept)


async def delete_works(session: AsyncSession, work_ids: Sequence[int]) -> None:
    """Delete works and every row that names them without a foreign key. Does not commit.

    The caller decides which works nobody would miss; this only makes sure that
    nothing polymorphic is left pointing at an id SQLite may hand out again.
    """

    for batch in in_batches(work_ids):
        editions = list(await session.scalars(select(Edition.id).where(Edition.work_id.in_(batch))))
        # Polymorphic, so no foreign key cascades these away.
        await _delete_provenance(session, EntityType.WORK, batch)
        for edition_batch in in_batches(editions):
            await _delete_provenance(session, EntityType.EDITION, edition_batch)
        # The rows only. Their files are the cover step's to sweep: it knows
        # which of them another row still points at.
        await session.execute(
            delete(ImageAsset).where(
                ImageAsset.entity_type == EntityType.WORK, ImageAsset.entity_id.in_(batch)
            )
        )
        await session.execute(
            delete(ExternalId).where(
                ExternalId.entity_type == EntityType.WORK, ExternalId.entity_id.in_(batch)
            )
        )
        # Editions and default state go with the work, by `ON DELETE CASCADE`.
        await session.execute(delete(Work).where(Work.id.in_(batch)))


async def _worth_keeping(session: AsyncSession, work_id: int) -> bool:
    work = await session.get_one(Work, work_id)
    if work.is_matched:
        return True
    if await session.scalar(select(exists().where(Work.parent_work_id == work_id))):
        return True
    for state in await session.scalars(
        select(UserWorkState).where(UserWorkState.work_id == work_id)
    ):
        if any(getattr(state, field) != default for field, default in USER_DEFAULTS.items()):
            return True
    editions = select(Edition.id).where(Edition.work_id == work_id)
    manual = await session.scalar(
        select(
            exists().where(
                FieldProvenance.source_kind == SourceKind.MANUAL,
                (
                    (FieldProvenance.entity_type == EntityType.WORK)
                    & (FieldProvenance.entity_id == work_id)
                )
                | (
                    (FieldProvenance.entity_type == EntityType.EDITION)
                    & FieldProvenance.entity_id.in_(editions)
                ),
            )
        )
    )
    return bool(manual)


async def _delete_provenance(
    session: AsyncSession, entity_type: EntityType, entity_ids: Sequence[int]
) -> None:
    await session.execute(
        delete(FieldProvenance).where(
            FieldProvenance.entity_type == entity_type, FieldProvenance.entity_id.in_(entity_ids)
        )
    )


async def _check_sole_source(session: AsyncSession, source_id: int, target_id: int) -> None:
    rows = await session.scalars(
        select(FieldProvenance).where(
            FieldProvenance.entity_type == EntityType.WORK,
            FieldProvenance.entity_id.in_([source_id, target_id]),
            FieldProvenance.sole_source,
        )
    )
    claims: dict[str, dict[int, str]] = {}
    for row in rows:
        claims.setdefault(row.field, {})[row.entity_id] = row.source_ref
    for field, by_work in sorted(claims.items()):
        if len(set(by_work.values())) > 1:
            raise MergeConflictError(
                f"work.{field} is single_source, and works {source_id} and {target_id} "
                f"take it from {by_work[source_id]} and {by_work[target_id]}"
            )


async def _adopt_children(session: AsyncSession, source: Work, target: Work) -> list[int]:
    """Repoint the source's children, and carry its own parent only where the target has none.

    Left undone, `ON DELETE RESTRICT` on the self-FK blocks the final delete.
    """

    if target.parent_work_id == source.id:
        # The target is the source's child: it cannot become its own parent.
        target.parent_work_id = None
    children = list(
        await session.scalars(
            select(Work.id).where(Work.parent_work_id == source.id, Work.id != target.id)
        )
    )
    if children:
        await session.execute(
            update(Work).where(Work.id.in_(children)).values(parent_work_id=target.id)
        )
    if target.parent_work_id is None and source.parent_work_id not in (None, target.id):
        target.parent_work_id = source.parent_work_id
    await session.flush()
    return children


async def _repoint_claims(
    session: AsyncSession, work_ids: Sequence[int], from_id: int, to_id: int | None
) -> list[int]:
    """Point every `parent_work_id` claim on these works that names `from_id` at `to_id`.

    The claim's value is a work id, so a merge that moves a column has to move
    the claim with it (#98). Returns the rows it changed, for the undo.
    """

    changed: list[int] = []
    for batch in in_batches(work_ids):
        for row in await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == EntityType.WORK,
                FieldProvenance.entity_id.in_(batch),
                FieldProvenance.field == "parent_work_id",
            )
        ):
            if row.value == from_id:
                row.value = to_id
                changed.append(row.id)
    await session.flush()
    return changed


async def _restore_claims(
    session: AsyncSession, row_ids: Sequence[int], from_value: int | None, to_id: int
) -> None:
    """`_repoint_claims` undone, for the rows that still say what the merge left them saying."""

    for batch in in_batches(row_ids):
        for row in await session.scalars(
            select(FieldProvenance).where(FieldProvenance.id.in_(batch))
        ):
            if row.value == from_value:
                row.value = to_id
    await session.flush()


async def _fold_editions(session: AsyncSession, source_id: int, target_id: int) -> dict[str, Any]:
    """Re-parent the source's editions, collapsing each one the target already has.

    An edition collapses into the target's edition with its slug, and the
    source's default into the target's default whatever it is called: a stub's
    `Standard` is a placeholder, not a claim about what was bought.
    """

    target_editions = list(
        await session.scalars(select(Edition).where(Edition.work_id == target_id))
    )
    by_slug = {edition.slug: edition for edition in target_editions}
    target_default = next((edition for edition in target_editions if edition.is_default), None)

    moved: list[int] = []
    collapsed: list[dict[str, Any]] = []
    for edition in await session.scalars(
        select(Edition).where(Edition.work_id == source_id).order_by(Edition.id)
    ):
        into = by_slug.get(edition.slug) or (target_default if edition.is_default else None)
        if into is None:
            edition.work_id = target_id
            moved.append(edition.id)
            continue
        entitlements = list(
            await session.scalars(
                select(Entitlement.id).where(Entitlement.edition_id == edition.id)
            )
        )
        if entitlements:
            await session.execute(
                update(Entitlement)
                .where(Entitlement.id.in_(entitlements))
                .values(edition_id=into.id)
            )
        provenance = await _fold_provenance(session, EntityType.EDITION, edition.id, into.id)
        collapsed.append(
            {
                "edition": _snapshot(edition),
                "into": into.id,
                "into_columns": _columns(into, provenance["fields"]),
                "entitlements": entitlements,
                "provenance": provenance,
            }
        )
        await session.delete(edition)
    await session.flush()
    for entry in collapsed:
        await _resolve_all(session, EntityType.EDITION, entry["into"])
    return {"moved": moved, "collapsed": collapsed}


async def _unfold_editions(
    session: AsyncSession, record: Mapping[str, Any], target_id: int, restored_id: int
) -> None:
    await _repoint_back(
        session, Edition.id, Edition.work_id, record["moved"], target_id, restored_id
    )
    for entry in record["collapsed"]:
        snapshot = entry["edition"]
        edition = _restore(Edition, {**snapshot, "work_id": restored_id})
        session.add(edition)
        await session.flush()
        if entry["entitlements"]:
            await session.execute(
                update(Entitlement)
                .where(
                    Entitlement.id.in_(entry["entitlements"]),
                    Entitlement.edition_id == entry["into"],
                )
                .values(edition_id=edition.id)
            )
        await _unfold_provenance(
            session, EntityType.EDITION, entry["provenance"], entry["into"], edition.id
        )
        into = await session.get(Edition, entry["into"])
        if into is not None:
            _put_back(into, entry["into_columns"])
        await session.flush()
        for edition_id in (entry["into"], edition.id):
            await _resolve_all(session, EntityType.EDITION, edition_id)


async def _move_links(session: AsyncSession, source_id: int, target_id: int) -> dict[str, Any]:
    """Every `entitlement_work` row of the source, onto the target.

    An entitlement already linked to the target keeps that row, and the source's
    is dropped; if the dropped one was its `primary`, the kept one takes the
    role, so the entitlement still says which work it belongs to (ADR-0013).
    """

    target_roles = {
        link.entitlement_id: link
        for link in await session.scalars(
            select(EntitlementWork).where(EntitlementWork.work_id == target_id)
        )
    }
    moved: list[int] = []
    dropped: list[dict[str, Any]] = []
    promote: list[EntitlementWork] = []
    for link in await session.scalars(
        select(EntitlementWork)
        .where(EntitlementWork.work_id == source_id)
        .order_by(EntitlementWork.entitlement_id)
    ):
        kept = target_roles.get(link.entitlement_id)
        if kept is None:
            moved.append(link.entitlement_id)
            continue
        raised = link.role is WorkLinkRole.PRIMARY and kept.role is not WorkLinkRole.PRIMARY
        dropped.append({"link": _snapshot(link), "target_role": kept.role if raised else None})
        if raised:
            promote.append(kept)
        await session.delete(link)
    # The dropped primary has to be gone before its successor takes the role:
    # the partial unique index allows one primary per entitlement at any moment.
    await session.flush()
    for link in promote:
        link.role = WorkLinkRole.PRIMARY
    for batch in in_batches(moved):
        await session.execute(
            update(EntitlementWork)
            .where(EntitlementWork.work_id == source_id, EntitlementWork.entitlement_id.in_(batch))
            .values(work_id=target_id)
        )
    await session.flush()
    return {"moved": moved, "dropped": dropped}


async def _unmove_links(
    session: AsyncSession, record: Mapping[str, Any], target_id: int, restored_id: int
) -> None:
    for batch in in_batches(record["moved"]):
        await session.execute(
            update(EntitlementWork)
            .where(EntitlementWork.work_id == target_id, EntitlementWork.entitlement_id.in_(batch))
            .values(work_id=restored_id)
        )
    # Demote first, for the same index `_move_links` works around.
    for entry in record["dropped"]:
        if entry["target_role"] is not None:
            await session.execute(
                update(EntitlementWork)
                .where(
                    EntitlementWork.work_id == target_id,
                    EntitlementWork.entitlement_id == entry["link"]["entitlement_id"],
                )
                .values(role=WorkLinkRole(entry["target_role"]))
            )
    await session.flush()
    for entry in record["dropped"]:
        session.add(_restore(EntitlementWork, {**entry["link"], "work_id": restored_id}))
    await session.flush()


async def _fold_provenance(
    session: AsyncSession, entity_type: EntityType, from_id: int, to_id: int
) -> dict[str, Any]:
    """Move one entity's provenance rows onto another, the later of two colliding rows surviving.

    Rows collide on `(field, source_kind, source_ref)`: one source's two
    statements about the same field, of which the later is the current one.
    Every moved row loses `is_effective`; the caller resolves the fields again.
    """

    incoming = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == entity_type, FieldProvenance.entity_id == from_id
            )
        )
    )
    if not incoming:
        return {"moved": [], "dropped": [], "fields": []}
    fields = sorted({row.field for row in incoming})
    resident = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == entity_type,
                FieldProvenance.entity_id == to_id,
                FieldProvenance.field.in_(fields),
            )
        )
    )
    by_key = {(row.field, row.source_kind, row.source_ref): row for row in resident}

    moving: list[FieldProvenance] = []
    losers: list[FieldProvenance] = []
    for row in incoming:
        rival = by_key.get((row.field, row.source_kind, row.source_ref))
        if rival is not None and rival.observed_at >= row.observed_at:
            losers.append(row)
            continue
        if rival is not None:
            losers.append(rival)
        moving.append(row)
    dropped = [_snapshot(row) for row in losers]
    for row in losers:
        await session.delete(row)
    await session.flush()

    # Two effective rows for one field on one entity is what the partial unique
    # index refuses, so every row in play is cleared before any row moves.
    for row in [*moving, *resident]:
        if row not in losers:
            row.is_effective = False
    await session.flush()
    for row in moving:
        row.entity_id = to_id
    await session.flush()
    return {"moved": [row.id for row in moving], "dropped": dropped, "fields": fields}


async def _unfold_provenance(
    session: AsyncSession,
    entity_type: EntityType,
    record: Mapping[str, Any],
    into_id: int,
    restored_id: int,
) -> None:
    """Move the rows back, then recreate the ones the fold deleted where each one was."""

    moved = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.id.in_(record["moved"]),
                FieldProvenance.entity_type == entity_type,
                FieldProvenance.entity_id == into_id,
            )
        )
    )
    fields = {row.field for row in moved} | {row["field"] for row in record["dropped"]}
    if fields:
        await session.execute(
            update(FieldProvenance)
            .where(
                FieldProvenance.entity_type == entity_type,
                FieldProvenance.entity_id == into_id,
                FieldProvenance.field.in_(fields),
            )
            .values(is_effective=False)
        )
    for row in moved:
        row.is_effective = False
        row.entity_id = restored_id
    await session.flush()
    for snapshot in record["dropped"]:
        # A deleted row was either the target's or the source's, and the
        # source's names an id that no longer exists.
        was_source = snapshot["entity_id"] != into_id
        session.add(
            _restore(
                FieldProvenance,
                {
                    **snapshot,
                    "entity_id": restored_id if was_source else into_id,
                    "is_effective": False,
                },
            )
        )
    await session.flush()


async def _merge_states(session: AsyncSession, source_id: int, target_id: int) -> dict[str, Any]:
    """User state per user: the source's fills what the target never set.

    A user with state only on the source keeps it whole, moved. `last_played_at`
    takes the later of the two, since both are true.
    """

    targets = {
        state.user_id: state
        for state in await session.scalars(
            select(UserWorkState).where(UserWorkState.work_id == target_id)
        )
    }
    moved: list[int] = []
    merged: list[dict[str, Any]] = []
    for state in await session.scalars(
        select(UserWorkState).where(UserWorkState.work_id == source_id)
    ):
        kept = targets.get(state.user_id)
        if kept is None:
            moved.append(state.user_id)
            continue
        previous: dict[str, Any] = {}
        for field, default in USER_DEFAULTS.items():
            ours, theirs = getattr(kept, field), getattr(state, field)
            if ours == default and theirs != default:
                previous[field] = _jsonable(ours)
                setattr(kept, field, theirs)
        if state.last_played_at is not None and (
            kept.last_played_at is None or state.last_played_at > kept.last_played_at
        ):
            previous["last_played_at"] = _jsonable(kept.last_played_at)
            kept.last_played_at = state.last_played_at
        merged.append({"state": _snapshot(state), "target_before": previous})
        await session.delete(state)
    await session.flush()
    if moved:
        await session.execute(
            update(UserWorkState)
            .where(UserWorkState.work_id == source_id, UserWorkState.user_id.in_(moved))
            .values(work_id=target_id)
        )
    await session.flush()
    return {"moved": moved, "merged": merged}


async def _unmerge_states(
    session: AsyncSession, record: Mapping[str, Any], target_id: int, restored_id: int
) -> None:
    if record["moved"]:
        await session.execute(
            update(UserWorkState)
            .where(UserWorkState.work_id == target_id, UserWorkState.user_id.in_(record["moved"]))
            .values(work_id=restored_id)
        )
    for entry in record["merged"]:
        snapshot = entry["state"]
        session.add(_restore(UserWorkState, {**snapshot, "work_id": restored_id}))
        kept = await session.get(UserWorkState, (snapshot["user_id"], target_id))
        if kept is not None:
            for field, value in _decoded(UserWorkState, entry["target_before"]).items():
                setattr(kept, field, value)
    await session.flush()


async def _move_images(session: AsyncSession, source_id: int, target_id: int) -> dict[str, Any]:
    """The source's images, onto the target, but not a file the target already has.

    Same file means same checksum, source and kind: one cover asserted twice.
    """

    held = {
        (image.kind, image.source_ref, image.checksum)
        for image in await session.scalars(
            select(ImageAsset).where(
                ImageAsset.entity_type == EntityType.WORK, ImageAsset.entity_id == target_id
            )
        )
        if image.checksum is not None
    }
    moved: list[int] = []
    dropped: list[dict[str, Any]] = []
    for image in await session.scalars(
        select(ImageAsset)
        .where(ImageAsset.entity_type == EntityType.WORK, ImageAsset.entity_id == source_id)
        .order_by(ImageAsset.id)
    ):
        if (image.kind, image.source_ref, image.checksum) in held:
            dropped.append(_snapshot(image))
            await session.delete(image)
        else:
            image.entity_id = target_id
            moved.append(image.id)
    await session.flush()
    return {"moved": moved, "dropped": dropped}


async def _unmove_images(
    session: AsyncSession, record: Mapping[str, Any], target_id: int, restored_id: int
) -> None:
    await _repoint_back(
        session, ImageAsset.id, ImageAsset.entity_id, record["moved"], target_id, restored_id
    )
    for snapshot in record["dropped"]:
        session.add(_restore(ImageAsset, {**snapshot, "entity_id": restored_id}))
    await session.flush()


async def _move_companies(session: AsyncSession, source_id: int, target_id: int) -> dict[str, Any]:
    """The source's company links, onto the target, but not one the target already has.

    The key is the whole row, so a link moves by being deleted and added again.
    Both kinds are kept whole for the undo, which cannot tell them apart otherwise.
    """

    held = {
        (link.company_id, link.role)
        for link in await session.scalars(
            select(WorkCompany).where(WorkCompany.work_id == target_id)
        )
    }
    moved: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for link in list(
        await session.scalars(select(WorkCompany).where(WorkCompany.work_id == source_id))
    ):
        (dropped if (link.company_id, link.role) in held else moved).append(_snapshot(link))
        await session.delete(link)
    await session.flush()
    for snapshot in moved:
        session.add(_restore(WorkCompany, {**snapshot, "work_id": target_id}))
    await session.flush()
    return {"moved": moved, "dropped": dropped}


async def _unmove_companies(
    session: AsyncSession, record: Mapping[str, Any], target_id: int, restored_id: int
) -> None:
    for snapshot in record["moved"]:
        await session.execute(
            delete(WorkCompany).where(
                WorkCompany.work_id == target_id,
                WorkCompany.company_id == snapshot["company_id"],
                WorkCompany.role == snapshot["role"],
            )
        )
    for snapshot in [*record["moved"], *record["dropped"]]:
        session.add(_restore(WorkCompany, {**snapshot, "work_id": restored_id}))
    await session.flush()


async def _move_genres(session: AsyncSession, source_id: int, target_id: int) -> dict[str, Any]:
    """The source's genres, onto the target, but not one the target already has.

    As `_move_companies`, keyed on the genre alone: a work is in a genre once,
    whichever source said so first.
    """

    held = set(
        await session.scalars(select(WorkGenre.genre_id).where(WorkGenre.work_id == target_id))
    )
    moved: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for link in list(
        await session.scalars(select(WorkGenre).where(WorkGenre.work_id == source_id))
    ):
        (dropped if link.genre_id in held else moved).append(_snapshot(link))
        await session.delete(link)
    await session.flush()
    for snapshot in moved:
        session.add(_restore(WorkGenre, {**snapshot, "work_id": target_id}))
    await session.flush()
    return {"moved": moved, "dropped": dropped}


async def _unmove_genres(
    session: AsyncSession, record: Mapping[str, Any], target_id: int, restored_id: int
) -> None:
    for snapshot in record["moved"]:
        await session.execute(
            delete(WorkGenre).where(
                WorkGenre.work_id == target_id, WorkGenre.genre_id == snapshot["genre_id"]
            )
        )
    for snapshot in [*record["moved"], *record["dropped"]]:
        session.add(_restore(WorkGenre, {**snapshot, "work_id": restored_id}))
    await session.flush()


async def _repoint(
    session: AsyncSession,
    key: InstrumentedAttribute[int],
    column: Reference,
    from_id: int,
    to_id: int,
    *where: ColumnElement[bool],
) -> list[int]:
    """Point `column` at `to_id` wherever it points at `from_id`; returns the rows' `key`s."""

    ids = list(await session.scalars(select(key).where(column == from_id, *where)))
    for batch in in_batches(ids):
        await session.execute(update(key.class_).where(key.in_(batch)).values({column: to_id}))
    return ids


async def _repoint_back(
    session: AsyncSession,
    key: InstrumentedAttribute[int],
    column: Reference,
    ids: Sequence[int],
    from_id: int,
    to_id: int,
) -> None:
    """`_repoint` undone, for the rows it named that still point where it left them."""

    for batch in in_batches(ids):
        await session.execute(
            update(key.class_).where(key.in_(batch), column == from_id).values({column: to_id})
        )


async def _folded_since(session: AsyncSession, merged: MatchAudit) -> MatchAudit | None:
    """The live merge that folded this merge's target away, if one did.

    Undone out of order, the payload's view of the target — its columns, the
    ids of the rows it moved — would be applied to whatever the target became
    later, taking a user's edit made in between off the work it now belongs
    to (rule 3). That merge repointed this row, so it holds the same `work_id`
    and names this row in its payload.
    """

    for later in await session.scalars(
        select(MatchAudit)
        .where(
            MatchAudit.action == MatchAction.MERGED,
            MatchAudit.work_id == merged.work_id,
            MatchAudit.id > merged.id,
        )
        .order_by(MatchAudit.id.desc())
    ):
        if merged.id in (later.details or {}).get("audits", ()) and not await _undone(
            session, later
        ):
            return later
    return None


async def _undone(session: AsyncSession, merged: MatchAudit) -> bool:
    """Whether an `unmerged` row names this merge.

    By `undoes` alone: the `unmerged` row's `previous_work_id` is the target as
    it was, and a later merge of that target repoints the `merged` row without
    touching it, so the two stop matching (#68 review).
    """

    return any(
        (unmerged.details or {}).get("undoes") == merged.id
        for unmerged in await session.scalars(
            select(MatchAudit).where(MatchAudit.action == MatchAction.UNMERGED)
        )
    )


async def _resolve_all(session: AsyncSession, entity_type: EntityType, entity_id: int) -> None:
    fields = list(
        await session.scalars(
            select(FieldProvenance.field)
            .where(
                FieldProvenance.entity_type == entity_type, FieldProvenance.entity_id == entity_id
            )
            .distinct()
            .order_by(FieldProvenance.field)
        )
    )
    if fields:
        await resolve(session, entity_type=entity_type, entity_id=entity_id, fields=fields)


async def _aggregate(session: AsyncSession, work_ids: Sequence[int]) -> None:
    """`playtime_minutes` summed again: the entitlements behind each work changed (rule 5)."""

    by_user: dict[int, list[int]] = {}
    for user_id, work_id in await session.execute(
        select(UserWorkState.user_id, UserWorkState.work_id).where(
            UserWorkState.work_id.in_(work_ids)
        )
    ):
        by_user.setdefault(user_id, []).append(work_id)
    for user_id, ids in sorted(by_user.items()):
        await resolve_work_aggregates_many(session, work_ids=ids, user_id=user_id)


def _jsonable(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


def _columns(row: Base, fields: Sequence[str]) -> dict[str, Any]:
    return {field: _jsonable(getattr(row, field)) for field in fields}


def _put_back(row: Base, columns: Mapping[str, Any]) -> None:
    for field, value in _decoded(type(row), columns).items():
        setattr(row, field, value)


def _snapshot(row: Base) -> dict[str, Any]:
    """Every column of a row, as JSON can hold it."""

    return {attr.key: _jsonable(getattr(row, attr.key)) for attr in inspect(type(row)).column_attrs}


def _decoded(model: type[Base], values: Mapping[str, Any]) -> dict[str, Any]:
    """`_snapshot` in reverse, column by column."""

    columns = {attr.key: attr.columns[0] for attr in inspect(model).column_attrs}
    decoded: dict[str, Any] = {}
    for key, value in values.items():
        # A `TypeDecorator` answers for its `impl`: `UtcDateTime` is a datetime.
        column_type = getattr(columns[key].type, "impl_instance", columns[key].type)
        try:
            python_type = column_type.python_type
        except NotImplementedError:
            python_type = object
        if isinstance(value, str) and python_type is not str:
            if issubclass(python_type, datetime):
                value = datetime.fromisoformat(value)
            elif issubclass(python_type, date):
                value = date.fromisoformat(value)
            elif issubclass(python_type, Enum):
                value = python_type(value)
        decoded[key] = value
    return decoded


# Left out of a restored row: the id, which is new, and the folded keys, which
# the models' validators compute from the titles and nothing sets directly.
NOT_RESTORED: Final = frozenset({"id", "sort_key", "title_key", "provider_title_key"})


def _restore[M: Base](model: type[M], snapshot: Mapping[str, Any]) -> M:
    """A new row from a snapshot, under a new id where the table has one.

    See `NOT_RESTORED` for what is not copied back, and why.
    """

    values = {key: value for key, value in snapshot.items() if key not in NOT_RESTORED}
    return model(**_decoded(model, values))
