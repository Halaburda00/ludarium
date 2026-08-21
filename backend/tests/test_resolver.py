import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_account, make_entitlement, make_provider, make_user, make_work
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import resolver
from ludarium.db import Database
from ludarium.enums import EntityType, FieldStrategy, ItemKind, SourceKind
from ludarium.models import Account, Base, EntitlementWork, FieldProvenance, UserWorkState, Work
from ludarium.models.types import ScalarValue
from ludarium.resolver import (
    STRATEGIES,
    ResolutionError,
    UnknownFieldError,
    picker_for,
    record,
    record_many,
    resolve,
    resolve_work_aggregates,
    resolve_work_aggregates_many,
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

    async def interrupted(session: AsyncSession, rows: Sequence[FieldProvenance]) -> None:
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


async def test_an_enum_column_holds_a_member_right_away(session: AsyncSession) -> None:
    """No commit, no refresh: whatever reads the entity next gets what a load would give.

    A provenance value is a JSON scalar, so the string `"dlc"` is what wins.
    Assigned raw it would sit on the attribute as a `str` until the next load,
    and an API response built in the same request would serialise that instead
    of the member.
    """

    work = await make_work(session, title="Hearts of Stone")
    await observe(
        session, work, source_kind=SourceKind.METADATA_PROVIDER, source_ref="igdb", value="dlc"
    )

    await resolve_work(session, work)

    assert work.item_kind is ItemKind.DLC


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


async def test_a_deferred_field_refuses_the_write_as_well(session: AsyncSession) -> None:
    """A row nothing can resolve is not a head start; it is a value that reads as missing."""

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)

    with pytest.raises(NotImplementedError, match="local agent"):
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="installed",
            source_kind=SourceKind.LOCAL_AGENT,
            source_ref="agent",
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

    assert written == {"playtime_minutes": None}
    assert entitlement.playtime_minutes is None


async def test_withdrawn_values_take_the_column_with_them(session: AsyncSession) -> None:
    """The column is a cache of the decision, so it may not outlive what it cached.

    Both sources reported a figure and then stopped. Leaving 3600 on the entity
    would show the user a number no row stands behind — and the flag would stay
    on a row whose own value is now null, so the detail view would credit the
    agent with a figure it has withdrawn.
    """

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    sources = ((SourceKind.PLATFORM_API, "steam"), (SourceKind.LOCAL_AGENT, "agent"))
    for (source_kind, source_ref), value in zip(sources, (3200, 3600), strict=True):
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="playtime_minutes",
            source_kind=source_kind,
            source_ref=source_ref,
            value=value,
        )
    await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=["playtime_minutes"],
    )
    assert entitlement.playtime_minutes == 3600

    for source_kind, source_ref in sources:
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="playtime_minutes",
            source_kind=source_kind,
            source_ref=source_ref,
            value=None,
        )
    written = await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=["playtime_minutes"],
    )

    assert written == {"playtime_minutes": None}
    assert entitlement.playtime_minutes is None
    assert await effective(session) == []


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


async def test_a_second_source_for_a_single_source_field_is_refused(
    session: AsyncSession,
) -> None:
    """Refused where it happens, so a manual override cannot hide it.

    At resolve time the override wins before any strategy runs, and the check
    would never fire for a field the user has edited — a provider writing where
    it must not would stay invisible for exactly the entitlements someone looked
    at closely enough to correct.
    """

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    await record(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        field="provider_title",
        source_kind=SourceKind.MANUAL,
        source_ref="manual",
        value="My own title",
    )
    await record(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        field="provider_title",
        source_kind=SourceKind.PLATFORM_API,
        source_ref="steam",
        value="The Witcher 3",
    )

    with pytest.raises(ResolutionError, match="steam already asserts it"):
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field="provider_title",
            source_kind=SourceKind.METADATA_PROVIDER,
            source_ref="igdb",
            value="The Witcher 3: Wild Hunt",
        )


def test_a_strategy_still_refuses_two_sources_it_should_never_see() -> None:
    """The backstop behind `record()`, for rows written before that guard existed."""

    rows = [
        FieldProvenance(
            entity_type=EntityType.ENTITLEMENT,
            entity_id=1,
            field="provider_title",
            source_kind=kind,
            source_ref=ref,
            value="The Witcher 3",
        )
        for kind, ref in (
            (SourceKind.PLATFORM_API, "steam"),
            (SourceKind.METADATA_PROVIDER, "igdb"),
        )
    ]

    with pytest.raises(ResolutionError, match="single_source"):
        picker_for(FieldStrategy.SINGLE_SOURCE)(rows)


def test_a_user_only_field_is_not_resolved_at_all() -> None:
    """Nothing sources these, so there is no decision to make — and none to fake."""

    with pytest.raises(ResolutionError, match="written by the user"):
        picker_for(FieldStrategy.USER_ONLY)


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


async def test_the_flag_says_what_the_registry_says(session: AsyncSession) -> None:
    """`sole_source` is `STRATEGIES` written down, so the index has something to be unique over.

    The manual row is deliberately not flagged: rule 3 puts the user above the
    strategy, so an override has to be able to sit beside the source it
    overrides.
    """

    work = await make_work(session)
    for field, source_kind, source_ref in (
        ("title", SourceKind.METADATA_PROVIDER, "igdb"),
        ("title", SourceKind.MANUAL, "manual"),
        ("item_kind", SourceKind.METADATA_PROVIDER, "igdb"),
    ):
        await record(
            session,
            entity_type=EntityType.WORK,
            entity_id=work.id,
            field=field,
            source_kind=source_kind,
            source_ref=source_ref,
            value="Prey" if field == "title" else ItemKind.GAME,
        )

    flags = {
        (row.field, row.source_ref): row.sole_source
        for row in await session.scalars(select(FieldProvenance))
    }
    assert flags == {
        ("title", "igdb"): True,
        ("title", "manual"): False,
        ("item_kind", "igdb"): False,
    }


async def test_the_database_refuses_the_second_source_on_its_own(session: AsyncSession) -> None:
    """What issue #20 found the schema unable to say, said as a constraint.

    `record()` refuses this pair long before the flush, so the only way to reach
    the index is to write the rows the way a lost race would — both decided
    against a database in which neither existed yet.
    """

    work = await make_work(session)
    for source_ref in ("igdb", "rawg"):
        session.add(
            FieldProvenance(
                entity_type=EntityType.WORK,
                entity_id=work.id,
                field="title",
                source_kind=SourceKind.METADATA_PROVIDER,
                source_ref=source_ref,
                value=f"Prey, according to {source_ref}",
                sole_source=True,
            )
        )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_two_runs_racing_one_single_source_field_end_in_one_row(db: Database) -> None:
    """Two interleaved sessions, and a decided outcome rather than whichever committed last.

    Rule 4 makes the contention legal — providers sync independently — and
    before ADR-0016 both read the field as unclaimed and both inserted. The
    second `BEGIN IMMEDIATE` now waits for the first to commit, so the loser's
    own `SELECT` finds the rival and `record()` names it. Which of the two wins
    is not asserted, because the transaction decides that and the outcome is
    the same either way.
    """

    async with db.writing_session_factory() as setup:
        work = Work(title="Prey", sort_title="prey")
        setup.add(work)
        await setup.commit()
        work_id = work.id

    async def claim(source_ref: str) -> str | None:
        async with db.writing_session_factory() as session:
            try:
                await record(
                    session,
                    entity_type=EntityType.WORK,
                    entity_id=work_id,
                    field="title",
                    source_kind=SourceKind.METADATA_PROVIDER,
                    source_ref=source_ref,
                    value=f"Prey, according to {source_ref}",
                )
            except ResolutionError as exc:
                return str(exc)
            # Held open, so the other one is certainly waiting at its BEGIN
            # rather than passing through before this one has committed.
            await asyncio.sleep(0.05)
            await session.commit()
        return None

    refusals = [
        refusal for refusal in await asyncio.gather(claim("igdb"), claim("rawg")) if refusal
    ]

    async with db.session_factory() as reader:
        rows = list(await reader.scalars(select(FieldProvenance)))
    assert len(refusals) == 1
    assert len(rows) == 1
    assert f"{rows[0].source_ref} already asserts it" in refusals[0]


@pytest.mark.parametrize("strategy", list(FieldStrategy))
@pytest.mark.parametrize("source_kind", list(SourceKind))
def test_the_refusal_and_the_flag_are_one_answer(
    strategy: FieldStrategy, source_kind: SourceKind
) -> None:
    """The two ends of `single_source`, which must never be given different rules.

    `record()` refuses a second source at runtime and flags the first one for
    the partial unique index. Were the exclusion list to grow a second
    `SourceKind` in one of the two places only, the database would go on
    enforcing a rule the code had stopped holding — and the disagreement would
    surface as an `IntegrityError` on a write the guard had just allowed.
    """

    rival = FieldProvenance(
        entity_type=EntityType.WORK,
        entity_id=1,
        field="title",
        source_kind=SourceKind.METADATA_PROVIDER,
        source_ref="igdb",
        value="Prey",
    )

    try:
        resolver._check_sole_source(
            [rival], strategy, EntityType.WORK, "title", source_kind, "rawg"
        )
        refused = False
    except ResolutionError:
        refused = True

    assert refused is resolver._claims_sole_source(strategy, source_kind)


async def test_recording_the_fields_together_says_what_recording_them_apart_said(
    session: AsyncSession,
) -> None:
    """The batch form is an optimisation, so it has to be indistinguishable (#23).

    Two works given the same three assertions, one field at a time and all three
    at once, compared on everything a later resolve reads.
    """

    apart = await make_work(session, title="Apart")
    together = await make_work(session, title="Together")
    values: dict[str, ScalarValue | None] = {
        "item_kind": "dlc",
        "release_year": 2015,
        "summary": None,
    }

    for field, value in values.items():
        await record(
            session,
            entity_type=EntityType.WORK,
            entity_id=apart.id,
            field=field,
            source_kind=SourceKind.METADATA_PROVIDER,
            source_ref="igdb",
            value=value,
        )
    recorded = await record_many(
        session,
        entity_type=EntityType.WORK,
        entity_id=together.id,
        source_kind=SourceKind.METADATA_PROVIDER,
        source_ref="igdb",
        values=values,
    )

    def described(rows: list[FieldProvenance]) -> set[tuple[str, ScalarValue | None, bool, bool]]:
        return {(row.field, row.value, row.sole_source, row.is_effective) for row in rows}

    stored = list(
        await session.scalars(select(FieldProvenance).where(FieldProvenance.entity_id == apart.id))
    )
    assert described([row for rows in recorded.values() for row in rows]) == described(stored)


async def test_the_batch_form_refuses_what_the_single_form_refuses(session: AsyncSession) -> None:
    """A field the registry does not describe, named among two that it does.

    Checked before anything is written, so the caller does not have to reason
    about which of its fields landed.
    """

    work = await make_work(session)

    with pytest.raises(UnknownFieldError, match=r"nothing resolves work\.publisher"):
        await record_many(
            session,
            entity_type=EntityType.WORK,
            entity_id=work.id,
            source_kind=SourceKind.METADATA_PROVIDER,
            source_ref="igdb",
            values={"item_kind": "game", "publisher": "CD Projekt", "release_year": 2015},
        )

    assert await session.scalar(select(func.count()).select_from(FieldProvenance)) == 0


async def test_resolve_given_the_rows_decides_what_it_would_have_read(
    session: AsyncSession,
) -> None:
    """The handed-over rows are the ones a second `SELECT` would have returned (#23).

    Two works with the same two sources disagreeing, resolved both ways.
    """

    read = await make_work(session, title="Read")
    handed = await make_work(session, title="Handed")
    for work in (read, handed):
        await record(
            session,
            entity_type=EntityType.WORK,
            entity_id=work.id,
            field="item_kind",
            source_kind=SourceKind.METADATA_PROVIDER,
            source_ref="igdb",
            value="dlc",
        )
    recorded = await record_many(
        session,
        entity_type=EntityType.WORK,
        entity_id=handed.id,
        source_kind=SourceKind.PLATFORM_API,
        source_ref="steam",
        values={"item_kind": "game"},
    )
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=read.id,
        field="item_kind",
        source_kind=SourceKind.PLATFORM_API,
        source_ref="steam",
        value="game",
    )

    without = await resolve(
        session, entity_type=EntityType.WORK, entity_id=read.id, fields=["item_kind"]
    )
    with_rows = await resolve(
        session,
        entity_type=EntityType.WORK,
        entity_id=handed.id,
        fields=["item_kind"],
        recorded=recorded,
    )

    # `platform_api` outranks `metadata_provider`, and the losing row is cleared
    # either way — the ladder is what decided, not which statement read the rows.
    assert without == with_rows == {"item_kind": "game"}
    winners = {row.entity_id: row.source_ref for row in await effective(session)}
    assert winners == {read.id: "steam", handed.id: "steam"}


async def test_many_works_aggregate_to_what_each_of_them_would(session: AsyncSession) -> None:
    """Including the one nothing is left owning, which is zero rather than untouched.

    The grouped query returns no row at all for such a work, so "no group" and
    "no playtime" have to mean the same thing here (rule 1).
    """

    account = await make_account(session)
    played = await make_work(session, title="Played")
    removed = await make_work(session, title="Removed")
    untouched = await make_work(session, title="Untouched")
    for work, minutes, gone in ((played, 2400, False), (removed, 999, True)):
        entitlement = await make_entitlement(session, account, provider_item_id=work.title)
        entitlement.playtime_minutes = minutes
        if gone:
            entitlement.removed_at = datetime.now(UTC)
        session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    for work in (played, removed, untouched):
        session.add(UserWorkState(work_id=work.id, playtime_minutes=7))
    await session.flush()

    states = await resolve_work_aggregates_many(
        session, work_ids=[played.id, removed.id, untouched.id, played.id], user_id=1
    )

    # Four ids in, three states out: the repeat is one work, not two.
    assert [state.work_id for state in states] == [played.id, removed.id, untouched.id]
    assert [state.playtime_minutes for state in states] == [2400, 0, 0]


async def test_aggregating_a_work_with_no_state_row_names_it(session: AsyncSession) -> None:
    """One missing row refuses the batch rather than quietly skipping it."""

    await make_user(session)
    work = await make_work(session)
    session.add(UserWorkState(work_id=work.id))
    await session.flush()
    orphan = await make_work(session, title="No State")

    with pytest.raises(ResolutionError, match=f"work {orphan.id}"):
        await resolve_work_aggregates_many(session, work_ids=[work.id, orphan.id], user_id=1)


async def test_aggregating_nothing_asks_the_database_nothing(session: AsyncSession) -> None:
    assert await resolve_work_aggregates_many(session, work_ids=[], user_id=1) == []
