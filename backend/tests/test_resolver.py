from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_account, make_entitlement, make_provider, make_user, make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import resolver
from ludarium.enums import EntityType, FieldStrategy, ItemKind, SourceKind
from ludarium.models import Account, Base, EntitlementWork, FieldProvenance, UserWorkState, Work
from ludarium.models.types import ScalarValue
from ludarium.resolver import (
    STRATEGIES,
    ResolutionError,
    UnknownFieldError,
    picker_for,
    record,
    resolve,
    resolve_work_aggregates,
)


async def observe(
    session: AsyncSession,
    work: Work,
    *,
    source_kind: SourceKind,
    source_ref: str,
    value: ScalarValue | None,
    field: str = "item_kind",
) -> FieldProvenance:
    return await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field=field,
        source_kind=source_kind,
        source_ref=source_ref,
        value=value,
    )


async def resolve_work(session: AsyncSession, work: Work, field: str = "item_kind") -> ScalarValue:
    written = await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=[field])
    return written[field]


async def effective(session: AsyncSession) -> list[FieldProvenance]:
    return list(await session.scalars(select(FieldProvenance).where(FieldProvenance.is_effective)))


async def test_a_manual_value_survives_a_later_sync(session: AsyncSession) -> None:
    """Rule 3, as the mechanism rather than the promise.

    Steam sells Hearts of Stone as a standalone product and the user says it is
    DLC. A sync writes its own row and cannot reach the manual one, so the only
    way the platform could win is if the resolver let it.
    """

    work = await make_work(session, title="Hearts of Stone")
    await observe(session, work, source_kind=SourceKind.MANUAL, source_ref="manual", value="dlc")
    await resolve_work(session, work)

    await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )
    await resolve_work(session, work)
    await session.commit()
    session.expunge_all()

    stored = await session.get(Work, work.id)
    assert stored is not None
    assert stored.item_kind is ItemKind.DLC
    winners = await effective(session)
    assert [(row.source_ref, row.value) for row in winners] == [("manual", "dlc")]


async def test_resolving_twice_leaves_one_winner(session: AsyncSession) -> None:
    """The partial unique index would catch a second winner; nothing would catch none."""

    work = await make_work(session)
    await observe(
        session, work, source_kind=SourceKind.METADATA_PROVIDER, source_ref="igdb", value="dlc"
    )
    await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )

    first = await resolve_work(session, work)
    second = await resolve_work(session, work)
    await session.commit()

    assert first == second == "game"
    winners = await effective(session)
    assert [row.source_ref for row in winners] == ["steam"]


async def test_a_better_source_takes_the_field_over(session: AsyncSession) -> None:
    """The handover the ordering exists for: clear the old winner, then set the new one.

    Marking the new one first would put two winners on the field for the length
    of one statement, and the partial unique index refuses that.
    """

    work = await make_work(session)
    await observe(
        session, work, source_kind=SourceKind.METADATA_PROVIDER, source_ref="igdb", value="dlc"
    )
    assert await resolve_work(session, work) == "dlc"

    await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )

    assert await resolve_work(session, work) == "game"
    winners = await effective(session)
    assert [row.source_ref for row in winners] == ["steam"]


async def test_an_interrupted_resolve_keeps_the_previous_winner(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure is injected at the one step the index cannot cover.

    Clearing the old winner and setting the new one are two statements, and
    between them the field has none. That window is why the whole resolve is one
    transaction: interrupted, it has to look like it never ran.
    """

    work = await make_work(session)
    work_id = work.id
    await observe(
        session, work, source_kind=SourceKind.METADATA_PROVIDER, source_ref="igdb", value="dlc"
    )
    await resolve_work(session, work)
    await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )
    await session.commit()

    async def interrupted(session: AsyncSession, row: FieldProvenance) -> None:
        raise RuntimeError("the process died here")

    monkeypatch.setattr(resolver, "_mark_effective", interrupted)

    with pytest.raises(RuntimeError):
        await resolve_work(session, work)
    await session.rollback()
    session.expunge_all()

    stored = await session.get(Work, work_id)
    assert stored is not None
    assert stored.item_kind is ItemKind.DLC
    winners = await effective(session)
    assert [row.source_ref for row in winners] == ["igdb"]


async def test_precedence_follows_the_ladder(session: AsyncSession) -> None:
    work = await make_work(session)
    await observe(
        session, work, source_kind=SourceKind.METADATA_PROVIDER, source_ref="igdb", value="dlc"
    )
    await observe(
        session, work, source_kind=SourceKind.LOCAL_AGENT, source_ref="agent", value="demo"
    )
    await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )

    assert await resolve_work(session, work) == "game"


async def test_a_tie_in_the_ladder_goes_to_the_heavier_provider(session: AsyncSession) -> None:
    """Both are platform APIs, so the ladder says nothing and the weight decides."""

    await make_provider(session, "steam", precedence_weight=100)
    await make_provider(session, "gog", precedence_weight=120)
    work = await make_work(session)
    await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )
    await observe(session, work, source_kind=SourceKind.PLATFORM_API, source_ref="gog", value="dlc")

    assert await resolve_work(session, work) == "dlc"


async def test_an_account_carries_its_provider_weight(session: AsyncSession) -> None:
    """`source_ref` is `account:12` where one provider has several accounts."""

    await make_user(session)
    provider = await make_provider(session, "steam", precedence_weight=130)
    await make_provider(session, "gog", precedence_weight=120)
    account = Account(provider_id=provider.id, label="Main")
    session.add(account)
    await session.flush()
    work = await make_work(session)
    await observe(
        session,
        work,
        source_kind=SourceKind.PLATFORM_API,
        source_ref=f"account:{account.id}",
        value="dlc",
    )
    await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="gog", value="game"
    )

    assert await resolve_work(session, work) == "dlc"


async def test_the_newest_observation_breaks_a_remaining_tie(session: AsyncSession) -> None:
    work = await make_work(session)
    older = await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )
    await observe(session, work, source_kind=SourceKind.PLATFORM_API, source_ref="gog", value="dlc")
    older.observed_at = datetime.now(UTC) - timedelta(days=1)
    await session.flush()

    assert await resolve_work(session, work) == "dlc"


async def test_a_source_with_nothing_to_say_can_still_win(session: AsyncSession) -> None:
    """A null value is an assertion; having no row is not. The distinction is the point."""

    work = await make_work(session, title="Portal 2")
    await observe(
        session,
        work,
        source_kind=SourceKind.METADATA_PROVIDER,
        source_ref="igdb",
        value=2011,
        field="release_year",
    )
    await observe(
        session,
        work,
        source_kind=SourceKind.MANUAL,
        source_ref="manual",
        value=None,
        field="release_year",
    )

    await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=["release_year"])
    await session.commit()
    session.expunge_all()

    stored = await session.get(Work, work.id)
    assert stored is not None
    assert stored.release_year is None


async def test_a_source_updates_its_own_row(session: AsyncSession) -> None:
    """One row per source per field: Steam changing its mind is not a second assertion."""

    work = await make_work(session)
    first = await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="game"
    )
    second = await observe(
        session, work, source_kind=SourceKind.PLATFORM_API, source_ref="steam", value="demo"
    )
    await session.commit()

    assert first.id == second.id
    assert [row.value for row in await session.scalars(select(FieldProvenance))] == ["demo"]


async def test_a_field_nothing_resolves_is_refused(session: AsyncSession) -> None:
    """The registry is the contract, so a provider cannot invent a field."""

    work = await make_work(session)

    with pytest.raises(UnknownFieldError, match=r"work\.rating"):
        await observe(
            session,
            work,
            source_kind=SourceKind.PLATFORM_API,
            source_ref="steam",
            value=9,
            field="rating",
        )


async def test_only_the_agent_reports_installed_state(session: AsyncSession) -> None:
    """Rule 5's field-level exception, refused at the write path rather than at resolve."""

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)

    with pytest.raises(ResolutionError, match="agent_only"):
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="installed",
            source_kind=SourceKind.PLATFORM_API,
            source_ref="steam",
            value=True,
        )


async def test_the_larger_playtime_wins(session: AsyncSession) -> None:
    """Within one entitlement the two sources describe the same play; the lower is stale."""

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    for source_kind, source_ref, value in (
        (SourceKind.PLATFORM_API, "steam", 3200),
        (SourceKind.LOCAL_AGENT, "agent", 3600),
    ):
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="playtime_minutes",
            source_kind=source_kind,
            source_ref=source_ref,
            value=value,
        )

    written = await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=["playtime_minutes"],
    )

    assert written["playtime_minutes"] == 3600
    assert entitlement.playtime_minutes == 3600


async def test_a_source_that_reports_no_playtime_decides_nothing(session: AsyncSession) -> None:
    """`max` needs a quantity, and an explicit "I have none" is not one."""

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    await record(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        field="playtime_minutes",
        source_kind=SourceKind.PLATFORM_API,
        source_ref="steam",
        value=None,
    )

    written = await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=["playtime_minutes"],
    )

    assert written == {}
    assert entitlement.playtime_minutes is None


async def test_one_source_settles_a_single_source_field(session: AsyncSession) -> None:
    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    await record(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        field="provider_title",
        source_kind=SourceKind.PLATFORM_API,
        source_ref="steam",
        value="The Witcher 3: Wild Hunt",
    )

    written = await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=["provider_title"],
    )

    assert written["provider_title"] == "The Witcher 3: Wild Hunt"


async def test_a_user_edit_wins_a_field_that_has_no_ladder(session: AsyncSession) -> None:
    """Rule 3 does not depend on the strategy, which is why the override runs before it.

    Left to `single_source` these two rows are a misclassified field and an
    error; the user editing a value is neither.
    """

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    for source_kind, source_ref, value in (
        (SourceKind.PLATFORM_API, "steam", "The Witcher 3"),
        (SourceKind.MANUAL, "manual", "The Witcher 3: Wild Hunt"),
    ):
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="provider_title",
            source_kind=source_kind,
            source_ref=source_ref,
            value=value,
        )

    written = await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=["provider_title"],
    )

    assert written["provider_title"] == "The Witcher 3: Wild Hunt"


async def test_a_single_source_field_with_two_sources_is_refused(session: AsyncSession) -> None:
    """Not a tie to break: the field was classified wrongly, and guessing hides that."""

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    for source_kind, source_ref in (
        (SourceKind.PLATFORM_API, "steam"),
        (SourceKind.METADATA_PROVIDER, "igdb"),
    ):
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="provider_title",
            source_kind=source_kind,
            source_ref=source_ref,
            value="The Witcher 3",
        )

    with pytest.raises(ResolutionError, match="single_source"):
        await resolve(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            fields=["provider_title"],
        )


async def test_a_provider_row_never_wins_a_user_only_field() -> None:
    row = FieldProvenance(
        entity_type=EntityType.WORK,
        entity_id=1,
        field="rating",
        source_kind=SourceKind.PLATFORM_API,
        source_ref="steam",
        value=9,
    )

    assert picker_for(FieldStrategy.USER_ONLY)([row]) is None


@pytest.mark.parametrize(
    ("strategy", "milestone"),
    [
        (FieldStrategy.LATEST, "M4"),
        (FieldStrategy.DERIVED, "M4"),
        (FieldStrategy.AGENT_ONLY, "local agent"),
    ],
)
def test_a_deferred_strategy_names_its_milestone(strategy: FieldStrategy, milestone: str) -> None:
    """A refusal that says when it will exist, rather than an untested branch."""

    with pytest.raises(NotImplementedError, match=milestone):
        picker_for(strategy)


def test_sum_is_not_a_choice_between_sources() -> None:
    with pytest.raises(ResolutionError, match="aggregates"):
        picker_for(FieldStrategy.SUM)


async def test_playtime_adds_up_across_entitlements(session: AsyncSession) -> None:
    """40 hours on Steam plus 20 on GOG is 60 hours played, not a disagreement."""

    work = await make_work(session)
    steam = await make_account(session)
    gog = await make_account(session, key="gog")
    for account, minutes, item in ((steam, 2400, "292030"), (gog, 1200, "1495134320")):
        entitlement = await make_entitlement(session, account, provider_item_id=item)
        entitlement.playtime_minutes = minutes
        session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    session.add(UserWorkState(work_id=work.id))
    await session.flush()

    state = await resolve_work_aggregates(session, work_id=work.id, user_id=1)

    assert state.playtime_minutes == 3600


async def test_a_removed_entitlement_stops_counting(session: AsyncSession) -> None:
    """Rule 1: the row and its playtime stay, the aggregate does not count them."""

    work = await make_work(session)
    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    entitlement.playtime_minutes = 2400
    session.add_all(
        [
            EntitlementWork(entitlement_id=entitlement.id, work_id=work.id),
            UserWorkState(work_id=work.id),
        ]
    )
    await session.flush()

    entitlement.removed_at = datetime.now(UTC)
    state = await resolve_work_aggregates(session, work_id=work.id, user_id=1)

    assert state.playtime_minutes == 0
    assert entitlement.playtime_minutes == 2400


async def test_aggregating_without_a_state_row_is_refused(session: AsyncSession) -> None:
    """The sync creates the row with the stub; resolving is not the place to invent one."""

    work = await make_work(session)

    with pytest.raises(ResolutionError, match="no user_work_state"):
        await resolve_work_aggregates(session, work_id=work.id, user_id=1)


async def test_resolving_an_entity_that_is_not_there_is_refused(session: AsyncSession) -> None:
    with pytest.raises(ResolutionError, match="no work with id"):
        await resolve(session, entity_type=EntityType.WORK, entity_id=404, fields=["item_kind"])


async def test_a_field_nobody_asserted_leaves_the_column_alone(session: AsyncSession) -> None:
    work = await make_work(session)

    assert (
        await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=["summary"])
        == {}
    )
    assert work.summary is None


def test_every_registry_field_is_a_column() -> None:
    """The registry names columns, and a rename that misses it would resolve nothing."""

    tables = {mapper.class_.__tablename__: mapper.class_ for mapper in Base.registry.mappers}

    for table, field in STRATEGIES:
        assert table in tables, table
        assert field in tables[table].__table__.columns, f"{table}.{field}"
