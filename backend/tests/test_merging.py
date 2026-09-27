from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import make_account, make_provider, make_work
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import (
    EntityType,
    ImageKind,
    ItemKind,
    MatchAction,
    MatchActor,
    MatchLayer,
    PlayStatus,
    SourceKind,
    WorkLinkRole,
)
from ludarium.merging import (
    MergeConflictError,
    MergeError,
    collect_orphan_stubs,
    merge_work,
    undo_merge,
)
from ludarium.models import (
    Account,
    Edition,
    Entitlement,
    EntitlementWork,
    ExternalId,
    FieldProvenance,
    ImageAsset,
    MatchAudit,
    UserWorkState,
    Work,
)
from ludarium.resolver import record, resolve, resolve_work_aggregates_many

EARLIER = datetime(2026, 9, 1, tzinfo=UTC)
LATER = EARLIER + timedelta(days=1)


@pytest.fixture
async def steam(session: AsyncSession) -> Account:
    return await make_account(session)


async def stub(
    session: AsyncSession,
    account: Account,
    appid: str | None,
    title: str,
    *,
    playtime: int | None = None,
    role: WorkLinkRole = WorkLinkRole.PRIMARY,
) -> Work:
    """What a sync leaves: a work, its `Standard` edition, a link and a default state."""

    work = await make_work(session, title)
    edition = Edition(work_id=work.id, name="Standard", slug="standard", is_default=True)
    session.add(edition)
    await session.flush()
    if appid is not None:
        await own(session, account, work, appid, playtime=playtime, role=role, edition=edition)
    session.add(UserWorkState(work_id=work.id))
    await session.flush()
    return work


async def own(
    session: AsyncSession,
    account: Account,
    work: Work,
    appid: str,
    *,
    playtime: int | None = None,
    role: WorkLinkRole = WorkLinkRole.PRIMARY,
    edition: Edition | None = None,
) -> Entitlement:
    entitlement = Entitlement(
        account_id=account.id,
        provider_item_id=appid,
        provider_title=work.title,
        playtime_minutes=playtime,
        edition_id=edition.id if edition is not None else None,
    )
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id, role=role))
    await session.flush()
    return entitlement


async def anchored(session: AsyncSession, account: Account, appid: str, title: str) -> Work:
    work = await stub(session, account, appid, title)
    work.igdb_id, work.is_matched = 1942, True
    session.add(
        ExternalId(entity_type=EntityType.WORK, entity_id=work.id, namespace="igdb", value="1942")
    )
    await session.flush()
    return work


async def assert_(
    session: AsyncSession,
    work: Work,
    field: str,
    value: Any,
    *,
    kind: SourceKind = SourceKind.METADATA_PROVIDER,
    ref: str = "igdb",
    at: datetime | None = None,
) -> FieldProvenance:
    row = await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field=field,
        source_kind=kind,
        source_ref=ref,
        value=value,
    )
    if at is not None:
        row.observed_at = at
    await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=[field])
    return row


def cover(work: Work, checksum: str, width: int) -> ImageAsset:
    return ImageAsset(
        entity_type=EntityType.WORK,
        entity_id=work.id,
        kind=ImageKind.COVER,
        source_ref="igdb",
        local_path=f"covers/igdb/{checksum}.jpg",
        checksum=checksum,
        width=width,
    )


async def merge(session: AsyncSession, source: Work, target: Work) -> MatchAudit:
    return await merge_work(
        session,
        source_id=source.id,
        target_id=target.id,
        layer=MatchLayer.HARD_ID,
        actor=MatchActor.AUTO,
    )


async def fresh[T](session: AsyncSession, query: Select[tuple[T]]) -> list[T]:
    """Rows as the database has them now, past whatever the identity map holds."""

    return list(await session.scalars(query.execution_options(populate_existing=True)))


async def links(session: AsyncSession) -> set[tuple[int, int, WorkLinkRole]]:
    return {
        (link.entitlement_id, link.work_id, link.role)
        for link in await fresh(session, select(EntitlementWork))
    }


async def state_of(session: AsyncSession, work: Work | int) -> UserWorkState:
    work_id = work if isinstance(work, int) else work.id
    return await session.get_one(UserWorkState, (1, work_id), populate_existing=True)


async def test_a_merge_moves_the_links_and_deletes_the_source(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3: Wild Hunt - GOTY")
    source_id = source.id

    audit = await merge(session, source, target)

    assert await session.get(Work, source_id) is None
    owned = {link[0]: link[1:] for link in await links(session)}
    assert set(owned.values()) == {(target.id, WorkLinkRole.PRIMARY)}
    assert (audit.action, audit.work_id, audit.previous_work_id, audit.layer, audit.actor) == (
        MatchAction.MERGED,
        target.id,
        source_id,
        MatchLayer.HARD_ID,
        MatchActor.AUTO,
    )


async def test_the_stub_s_standard_edition_collapses_into_the_target_s_default(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    default = await session.scalar(select(Edition).where(Edition.work_id == target.id))
    assert default is not None
    default.slug, default.name = "goty", "Game of the Year"
    source = await stub(session, steam, "499450", "The Witcher 3: Wild Hunt - GOTY")
    complete = Edition(work_id=source.id, name="Complete", slug="complete")
    session.add(complete)
    await session.flush()
    await own(session, steam, source, "1", edition=complete)

    await merge(session, source, target)

    editions = {
        edition.slug: edition
        for edition in await fresh(session, select(Edition).where(Edition.work_id == target.id))
    }
    assert set(editions) == {"goty", "complete"}
    assert [slug for slug, edition in editions.items() if edition.is_default] == ["goty"]
    bought = {entitlement.edition_id for entitlement in await fresh(session, select(Entitlement))}
    assert bought == {editions["goty"].id, editions["complete"].id}


async def test_playtime_is_summed_again_across_the_merged_entitlements(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    first = await session.scalar(select(Entitlement))
    assert first is not None
    first.playtime_minutes = 4200
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY", playtime=900)

    await merge(session, source, target)

    assert (await state_of(session, target)).playtime_minutes == 5100


async def test_a_colliding_provenance_row_keeps_the_later_observation(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    await assert_(session, target, "item_kind", "dlc", ref="steam_store", at=EARLIER)
    await assert_(session, source, "item_kind", "game", ref="steam_store", at=LATER)

    await merge(session, source, target)

    rows = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == EntityType.WORK,
                FieldProvenance.entity_id == target.id,
                FieldProvenance.field == "item_kind",
            )
        )
    )
    assert [(row.value, row.is_effective) for row in rows] == [("game", True)]
    await session.refresh(target)
    assert target.item_kind is ItemKind.GAME


async def test_a_title_the_user_set_on_the_stub_wins_on_the_target(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    await assert_(session, target, "title", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    await assert_(session, source, "title", "Wiedźmin 3", kind=SourceKind.MANUAL, ref="user")

    await merge(session, source, target)

    await session.refresh(target)
    assert target.title == "Wiedźmin 3"


async def test_two_single_source_claims_are_a_conflict_and_nothing_moves(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    await assert_(session, target, "metacritic_score", 92, ref="rawg")
    await assert_(session, source, "metacritic_score", 91, ref="opencritic")
    before = await links(session)

    with pytest.raises(MergeConflictError, match="metacritic_score"):
        await merge(session, source, target)

    assert await links(session) == before
    assert await session.scalar(select(func.count()).select_from(MatchAudit)) == 0


async def test_user_state_fills_only_what_the_target_left_at_its_default(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    kept = await state_of(session, target)
    kept.rating = 9
    folded = await state_of(session, source)
    folded.rating, folded.play_status, folded.notes = 4, PlayStatus.COMPLETED, "Blood and Wine"

    await merge(session, source, target)

    state = await state_of(session, target)
    assert (state.rating, state.play_status, state.notes) == (
        9,
        PlayStatus.COMPLETED,
        "Blood and Wine",
    )


async def test_children_move_and_the_source_s_parent_is_carried_where_the_target_has_none(
    session: AsyncSession, steam: Account
) -> None:
    parent = await make_work(session, "The Witcher 3: Wild Hunt")
    target = await anchored(session, steam, "378648", "Hearts of Stone")
    source = await stub(session, steam, "378649", "Hearts of Stone (stub)")
    child = await make_work(session, "Hearts of Stone soundtrack")
    source.parent_work_id, child.parent_work_id = parent.id, source.id
    await session.flush()

    await merge(session, source, target)

    await session.refresh(child)
    await session.refresh(target)
    assert (child.parent_work_id, target.parent_work_id) == (target.id, parent.id)


async def test_a_target_that_was_the_source_s_child_does_not_become_its_own_parent(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    target.parent_work_id = source.id
    await session.flush()

    await merge(session, source, target)

    await session.refresh(target)
    assert target.parent_work_id is None


async def test_an_entitlement_on_both_works_keeps_one_link_and_its_primary(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    bundle = await session.scalar(
        select(EntitlementWork.entitlement_id).where(EntitlementWork.work_id == source.id)
    )
    assert bundle is not None
    session.add(
        EntitlementWork(entitlement_id=bundle, work_id=target.id, role=WorkLinkRole.GRANTED)
    )
    await session.flush()

    await merge(session, source, target)

    assert (bundle, target.id, WorkLinkRole.PRIMARY) in await links(session)


async def test_external_ids_move_with_the_work(session: AsyncSession, steam: Account) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    session.add(
        ExternalId(entity_type=EntityType.WORK, entity_id=source.id, namespace="gog", value="14")
    )
    await session.flush()

    await merge(session, source, target)

    owners = await session.scalars(
        select(ExternalId.entity_id).execution_options(populate_existing=True)
    )
    assert set(owners) == {target.id}


async def test_an_anchored_source_and_a_merge_into_itself_are_refused(
    session: AsyncSession, steam: Account
) -> None:
    matched = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    other = await stub(session, steam, "499450", "The Witcher 3 GOTY")

    with pytest.raises(MergeError, match="anchored"):
        await merge(session, matched, other)
    with pytest.raises(MergeError, match="itself"):
        await merge(session, other, other)


async def world(session: AsyncSession, rename: dict[int, int] | None = None) -> dict[str, Any]:
    """Everything a merge touches, with work ids renamed and editions named by work and slug.

    An undo restores the source under a new id, so a comparison has to say
    which new id stands for which old one; editions are recreated too, and are
    compared by what they are rather than by id.
    """

    rename = rename or {}

    def w(work_id: int | None) -> int | None:
        return rename.get(work_id, work_id) if work_id is not None else None

    editions = {
        edition.id: (w(edition.work_id), edition.slug)
        for edition in await fresh(session, select(Edition))
    }

    def entity(entity_type: EntityType, entity_id: int) -> object:
        return editions[entity_id] if entity_type is EntityType.EDITION else w(entity_id)

    return {
        "works": {
            (
                w(work.id),
                work.title,
                work.sort_key,
                work.item_kind,
                w(work.parent_work_id),
                work.igdb_id,
                work.is_matched,
                work.created_at,
            )
            for work in await fresh(session, select(Work))
        },
        "editions": {
            (*editions[edition.id], edition.name, edition.is_default)
            for edition in await fresh(session, select(Edition))
        },
        "bought": {
            (entitlement.id, editions.get(entitlement.edition_id or -1))
            for entitlement in await fresh(session, select(Entitlement))
        },
        "links": {
            (link.entitlement_id, w(link.work_id), link.role, link.match_layer)
            for link in await fresh(session, select(EntitlementWork))
        },
        "provenance": {
            (
                row.entity_type,
                entity(row.entity_type, row.entity_id),
                row.field,
                row.source_kind,
                row.source_ref,
                row.value,
                row.is_effective,
                row.sole_source,
                row.observed_at,
            )
            for row in await fresh(session, select(FieldProvenance))
        },
        "images": {
            (w(image.entity_id), image.kind, image.source_ref, image.checksum, image.width)
            for image in await fresh(session, select(ImageAsset))
        },
        "external_ids": {
            (w(row.entity_id), row.namespace, row.value)
            for row in await fresh(session, select(ExternalId))
        },
        "states": {
            (
                w(state.work_id),
                state.play_status,
                state.rating,
                state.notes,
                state.is_favourite,
                state.playtime_minutes,
                state.last_played_at,
            )
            for state in await fresh(session, select(UserWorkState))
        },
    }


async def test_an_undo_puts_back_everything_the_merge_moved(
    session: AsyncSession, steam: Account
) -> None:
    """Rule 6: reversible, not only readable. Every kind of row a merge touches, round-tripped."""

    parent = await make_work(session, "The Witcher 3: Wild Hunt")
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    target_default = await session.scalar(select(Edition).where(Edition.work_id == target.id))
    assert target_default is not None
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY", playtime=900)
    source.parent_work_id = parent.id
    child = await make_work(session, "Blood and Wine")
    child.parent_work_id = source.id
    complete = Edition(work_id=source.id, name="Complete", slug="complete")
    session.add(complete)
    await session.flush()
    await own(session, steam, source, "1", playtime=30, edition=complete)
    # On both works, and primary on the source's side.
    bundle = await session.scalar(
        select(EntitlementWork.entitlement_id).where(EntitlementWork.work_id == source.id)
    )
    assert bundle is not None
    session.add(
        EntitlementWork(entitlement_id=bundle, work_id=target.id, role=WorkLinkRole.GRANTED)
    )
    await session.flush()
    await assert_(session, target, "title", "The Witcher 3: Wild Hunt")
    await assert_(session, target, "item_kind", "dlc", ref="steam_store", at=EARLIER)
    await assert_(session, source, "item_kind", "game", ref="steam_store", at=LATER)
    await assert_(session, source, "sort_title", "Witcher GOTY", kind=SourceKind.MANUAL, ref="user")
    await record(
        session,
        entity_type=EntityType.EDITION,
        entity_id=target_default.id,
        field="name",
        source_kind=SourceKind.METADATA_PROVIDER,
        source_ref="igdb",
        value="Standard Edition",
    )
    await resolve(
        session, entity_type=EntityType.EDITION, entity_id=target_default.id, fields=["name"]
    )
    session.add(
        ExternalId(entity_type=EntityType.WORK, entity_id=source.id, namespace="gog", value="14")
    )
    session.add_all(
        [
            cover(target, "same-file", 264),
            # One file the target already has, and one it does not.
            cover(source, "same-file", 264),
            cover(source, "own-file", 528),
        ]
    )
    (await state_of(session, source)).rating = 7
    (await state_of(session, target)).notes = "Replay"
    # The state a sync would have left, so that the undo's recount is compared
    # with a count rather than with a default.
    await resolve_work_aggregates_many(session, work_ids=[target.id, source.id], user_id=1)
    earlier = MatchAudit(
        work_id=source.id, action=MatchAction.LINKED, entitlement_id=bundle, actor=MatchActor.USER
    )
    session.add(earlier)
    await session.flush()
    before = await world(session)

    merged = await merge(session, source, target)
    assert await world(session) != before
    unmerged = await undo_merge(session, audit_id=merged.id, actor=MatchActor.USER)

    assert await world(session, {unmerged.work_id: source.id}) == before
    assert (unmerged.action, unmerged.previous_work_id, unmerged.details) == (
        MatchAction.UNMERGED,
        target.id,
        {"undoes": merged.id},
    )
    assert (await session.get_one(MatchAudit, earlier.id)).work_id == unmerged.work_id


async def test_a_merge_is_undone_once(session: AsyncSession, steam: Account) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    merged = await merge(session, source, target)
    unmerged = await undo_merge(session, audit_id=merged.id, actor=MatchActor.USER)

    with pytest.raises(MergeError, match="already been undone"):
        await undo_merge(session, audit_id=merged.id, actor=MatchActor.USER)
    with pytest.raises(MergeError, match="not a merge"):
        await undo_merge(session, audit_id=unmerged.id, actor=MatchActor.USER)


async def test_an_orphan_stub_is_deleted_with_its_provenance(
    session: AsyncSession, steam: Account
) -> None:
    orphan = await stub(session, steam, None, "Nobody owns this")
    await assert_(session, orphan, "item_kind", "game", ref="steam_store")
    owned = await stub(session, steam, "292030", "Owned")
    orphan_id, owned_id = orphan.id, owned.id

    report = await collect_orphan_stubs(session)

    assert (report.deleted, report.kept) == ([orphan_id], [])
    session.expire_all()
    assert set(await session.scalars(select(Work.id))) == {owned_id}
    assert await session.scalar(select(func.count()).select_from(FieldProvenance)) == 0
    assert await session.scalar(select(func.count()).select_from(Edition)) == 1


async def test_a_removed_entitlement_keeps_its_stub(session: AsyncSession, steam: Account) -> None:
    """Rule 1: the entitlement stays restorable, so the work it restores into stays too."""

    work = await stub(session, steam, "292030", "Refunded")
    entitlement = await session.scalar(select(Entitlement))
    assert entitlement is not None
    entitlement.removed_at = LATER

    assert (await collect_orphan_stubs(session)).deleted == []
    assert await session.get(Work, work.id) is not None


async def test_an_orphan_the_user_or_the_matcher_touched_is_kept(
    session: AsyncSession, steam: Account
) -> None:
    await make_provider(session, "igdb")
    matched = await stub(session, steam, None, "Matched")
    matched.is_matched = True
    rated = await stub(session, steam, None, "Rated")
    (await state_of(session, rated)).rating = 8
    renamed = await stub(session, steam, None, "Renamed")
    await assert_(session, renamed, "title", "My name", kind=SourceKind.MANUAL, ref="user")
    parent = await stub(session, steam, None, "Parent")
    child = await stub(session, steam, "292030", "Child")
    child.parent_work_id = parent.id
    await session.flush()

    report = await collect_orphan_stubs(session)

    assert report.deleted == []
    assert report.kept == [matched.id, rated.id, renamed.id, parent.id]


async def test_a_merge_stays_undone_when_its_target_is_merged_later(
    session: AsyncSession, steam: Account
) -> None:
    a = await stub(session, steam, "1", "A")
    b = await stub(session, steam, "2", "B")
    c = await stub(session, steam, "3", "C")
    first = await merge(session, a, b)
    await undo_merge(session, audit_id=first.id, actor=MatchActor.USER)
    later = await merge(session, b, c)
    await undo_merge(session, audit_id=later.id, actor=MatchActor.USER)

    with pytest.raises(MergeError, match="already been undone"):
        await undo_merge(session, audit_id=first.id, actor=MatchActor.USER)


async def test_a_merge_whose_target_was_merged_since_waits_for_that_merge_to_be_undone(
    session: AsyncSession, steam: Account
) -> None:
    """Undoing out of order would put the first merge's view of the target onto a later work."""

    a = await stub(session, steam, "1", "A")
    b = await stub(session, steam, "2", "B")
    c = await stub(session, steam, "3", "C")
    c_id = c.id
    await assert_(session, a, "title", "A's own", kind=SourceKind.MANUAL, ref="user")
    first = await merge(session, a, b)
    await assert_(session, b, "title", "The user's latest", kind=SourceKind.MANUAL, ref="user")
    second = await merge(session, b, c)

    with pytest.raises(MergeError, match=f"match_audit {second.id}"):
        await undo_merge(session, audit_id=first.id, actor=MatchActor.USER)
    assert (await session.get_one(Work, c_id, populate_existing=True)).title == "The user's latest"

    await undo_merge(session, audit_id=second.id, actor=MatchActor.USER)
    await undo_merge(session, audit_id=first.id, actor=MatchActor.USER)


async def test_a_cover_the_target_already_has_is_not_moved_twice(
    session: AsyncSession, steam: Account
) -> None:
    target = await anchored(session, steam, "292030", "The Witcher 3: Wild Hunt")
    source = await stub(session, steam, "499450", "The Witcher 3 GOTY")
    session.add_all(
        [cover(target, "same", 264), cover(source, "same", 264), cover(source, "x", 528)]
    )
    await session.flush()

    await merge(session, source, target)

    images = await fresh(session, select(ImageAsset))
    assert sorted((image.entity_id, image.checksum) for image in images) == [
        (target.id, "same"),
        (target.id, "x"),
    ]


async def test_an_orphan_stub_takes_its_image_rows_with_it(
    session: AsyncSession, steam: Account
) -> None:
    orphan = await stub(session, steam, None, "Nobody owns this")
    session.add(cover(orphan, "gone", 264))
    await session.flush()

    await collect_orphan_stubs(session)

    assert await session.scalar(select(func.count()).select_from(ImageAsset)) == 0
