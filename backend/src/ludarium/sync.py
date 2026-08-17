"""One run: ask a provider what an account owns and land it without losing anything.

The transaction boundaries are the design. The `sync_run` row is committed
before the fetch, so a process killed mid-run leaves a `running` row to explain
itself rather than no evidence at all; everything the run then changes lands in
a second transaction that commits together with `status = success`. There is no
moment where a partial result is committed and still looks like a finished sync,
which is what rule 1 needs from the write side.

Unlike the resolver, which leaves the transaction to its caller, this owns it.

Marking absent entitlements `removed_at` and updating the per-provider status
are deliberately not here: they are issue #8, behind this one.
"""

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import (
    EntitlementOrigin,
    EntityType,
    SyncStatus,
    SyncTrigger,
    WorkLinkRole,
)
from ludarium.models import (
    Account,
    Edition,
    Entitlement,
    EntitlementWork,
    Provider,
    SyncRun,
    UserWorkState,
    Work,
)
from ludarium.models.types import ScalarValue, utcnow
from ludarium.providers import LibraryItem, LibraryProvider, ProviderError
from ludarium.resolver import record, resolve, resolve_work_aggregates
from ludarium.titles import sort_title

# Every work has at least one edition, so a provider entry that says nothing
# about which one was bought still has something to attach to.
DEFAULT_EDITION_NAME: Final = "Standard"
DEFAULT_EDITION_SLUG: Final = "standard"


class SyncError(Exception):
    """The run could not start. Not a provider failure — those are a run status."""


@dataclass
class _Progress:
    """How many items the provider handed over, kept outside the session.

    A failed run rolls back, and the rollback takes the counters on the run row
    with it — correctly for `items_added`, which after a rollback really is
    zero, and wrongly for this one. The library did arrive; the run row is the
    only place that can still say so, and "2000 seen, 0 added" is the difference
    between a provider that answered and one that did not.
    """

    items_seen: int = 0


async def sync_account(
    session: AsyncSession,
    *,
    account: Account,
    library: LibraryProvider,
    trigger: SyncTrigger = SyncTrigger.MANUAL,
) -> SyncRun:
    """Run one sync and return it, finished either way.

    A provider failure is a status rather than an exception: rule 4 says an Epic
    outage must not affect a Steam sync, and a caller iterating over accounts
    should not have to catch anything to keep that true. `ProviderError` is the
    whole of a provider's failure vocabulary, so anything else is our bug —
    closing the run is all that happens to it before it is let out.
    """

    reporter = await _reporter(session, library.key)
    run = SyncRun(
        provider_id=reporter.id,
        account_id=account.id,
        trigger=trigger,
        status=SyncStatus.RUNNING,
    )
    session.add(run)
    await session.commit()

    seen = _Progress()
    try:
        items = await library.fetch_library()
        seen.items_seen = len(items)
        await _apply(session, run=run, account=account, reporter=reporter, items=items)
    except ProviderError as exc:
        # Safe to store: `ProviderError` never carries a credential, which is
        # the contract `providers.base` states rather than a hope (rule 7).
        await _close(session, run, seen, SyncStatus.FAILED, error=str(exc))
        return run
    except BaseException as exc:
        # `BaseException`, not `Exception`, for one reason: `CancelledError` has
        # not been an `Exception` since 3.8, so a caller putting a timeout
        # around this — `asyncio.wait_for`, an APScheduler job deadline — would
        # otherwise leave the row `running` forever, and "finished either way"
        # above would be a lie in the one case nobody is watching.
        #
        # Cancellation is still re-raised. Swallowing it would be the worse bug,
        # and the caller sees `TimeoutError` from `wait_for` regardless, so a
        # loop over several accounts carries on either way (rule 4).
        await _close(session, run, seen, SyncStatus.FAILED, error=type(exc).__name__)
        raise
    await _close(session, run, seen, SyncStatus.SUCCESS)
    return run


async def _reporter(session: AsyncSession, key: str) -> Provider:
    """Whoever is reporting, which is not always whoever owns the account.

    A Galaxy import reports for a Battle.net account, and it is the reporter's
    `source_kind` that lands on the provenance rows, not the account's
    (`docs/schema.md`). Taking it from the client's own key rather than from the
    account is what makes that the default instead of a later special case.
    """

    provider = await session.scalar(select(Provider).where(Provider.key == key))
    if provider is None:
        raise SyncError(f"no provider row for `{key}`; the seed is out of step with the code")
    return provider


async def _close(
    session: AsyncSession,
    run: SyncRun,
    seen: _Progress,
    status: SyncStatus,
    *,
    error: str | None = None,
) -> None:
    """Finish the run. Anything short of success leaves the library as it was found.

    The rollback is the enforcement of rule 1 in its plainest form, and it comes
    before the status is written so the two can never disagree: there is no
    committed state in which a run reports `failed` over changes it kept.

    `items_seen` is written after the rollback rather than surviving it, because
    it describes the provider's answer rather than anything this run wrote.
    """

    if status is not SyncStatus.SUCCESS:
        await session.rollback()
        await session.refresh(run)
    run.items_seen = seen.items_seen
    run.status = status
    run.finished_at = utcnow()
    run.error_text = error
    await session.commit()


async def _apply(
    session: AsyncSession,
    *,
    run: SyncRun,
    account: Account,
    reporter: Provider,
    items: list[LibraryItem],
) -> None:
    # `items_seen` is `_close`'s, so that a failed run keeps it. `items_removed`
    # stays 0: nothing here marks anything removed yet (#8).
    touched: list[int] = []
    for item in items:
        entitlement, created = await _upsert(session, account=account, item=item)
        await _assert_fields(
            session, run=run, reporter=reporter, entitlement=entitlement, item=item
        )
        if created:
            await _stub(session, run=run, account=account, entitlement=entitlement)
            run.items_added += 1
        else:
            run.items_updated += 1
        touched.append(entitlement.id)
    await _aggregate(session, user_id=account.user_id, entitlement_ids=touched)


async def _upsert(
    session: AsyncSession, *, account: Account, item: LibraryItem
) -> tuple[Entitlement, bool]:
    """Find or create the row for one owned item. Returns it and whether it is new.

    Keyed on `(account_id, provider_item_id)`, which is the unique index. Rows
    with `origin = manual` are excluded by predicate as well (rule 2). A CHECK
    constraint now makes it impossible for such a row to carry a
    `provider_item_id` at all, so the predicate is deliberately redundant: rule
    2 is worth two independent guards, and this is the one that says so where
    the query is written.
    """

    entitlement = await session.scalar(
        select(Entitlement).where(
            Entitlement.account_id == account.id,
            Entitlement.provider_item_id == item.provider_item_id,
            Entitlement.origin != EntitlementOrigin.MANUAL,
        )
    )
    created = entitlement is None
    if entitlement is None:
        entitlement = Entitlement(
            user_id=account.user_id,
            account_id=account.id,
            origin=EntitlementOrigin.SYNC,
            provider_item_id=item.provider_item_id,
            # Seeded because the column is NOT NULL and the row has to exist
            # before a provenance row can address it. The resolver owns it from
            # the next statement onwards.
            provider_title=item.title,
        )
        session.add(entitlement)
    _describe(entitlement, item)
    # Touched by every run that still sees the item; `first_seen_at` is not, so
    # that "owned since" survives every later run and a removal after it.
    entitlement.last_seen_at = utcnow()
    await session.flush()
    return entitlement, created


def _describe(entitlement: Entitlement, item: LibraryItem) -> None:
    """The columns no strategy governs, written straight.

    They carry no provenance row because nothing competes for them: `item_kind`
    here is the platform's own label and stays that way, with the resolved kind
    living on `work`. Contrast `playtime_minutes`, which the local agent will
    also report about the same account, which is why it goes through the
    registry instead.

    Written unconditionally, including nulls: with sync the only writer, the
    column should say what the last run said, not what some earlier one did.
    """

    entitlement.ownership_type = item.ownership_type
    entitlement.item_kind = item.item_kind
    entitlement.last_played_at = item.last_played_at
    entitlement.acquired_at = item.acquired_at
    entitlement.raw_payload = item.raw


async def _assert_fields(
    session: AsyncSession,
    *,
    run: SyncRun,
    reporter: Provider,
    entitlement: Entitlement,
    item: LibraryItem,
) -> None:
    """Rule 9: the provider states, the resolver decides, and only the resolver writes.

    One `source_ref` per provider is enough even once a platform has several
    accounts: an entitlement belongs to exactly one account, so two Steam
    accounts own two rows and never assert the same entitlement's fields.
    """

    values: dict[str, ScalarValue | None] = {
        "provider_item_id": item.provider_item_id,
        "provider_title": item.title,
        # None where the platform reported nothing, which the resolver reads as
        # "this source has no figure" rather than as zero minutes played.
        "playtime_minutes": item.playtime_minutes,
    }
    for field, value in values.items():
        await record(
            session,
            entity_type=EntityType.ENTITLEMENT,
            entity_id=entitlement.id,
            field=field,
            source_kind=reporter.source_kind,
            source_ref=reporter.key,
            value=value,
            run_id=run.id,
        )
    await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=list(values),
    )


async def _stub(
    session: AsyncSession, *, run: SyncRun, account: Account, entitlement: Entitlement
) -> Work:
    """A work, its default edition and the primary link, in this transaction (ADR-0015).

    So that no entitlement is ever work-less: the grid is work-centric from the
    first run, and `user_work_state` can hold a status for a game the matcher
    has never seen.

    The title is copied from the resolved `provider_title` rather than sourced
    from the platform. Platforms are not a source for `work.title` — a stub's
    name is a derived value with no provenance row behind it, which is how the
    IGDB anchor replaces it in M2 without having to outrank anyone.

    `normalised_title` stays null: that is `ludamatch`'s output and it lives in
    another repository (M2).
    """

    title = entitlement.provider_title.strip() or _nameless(entitlement)
    work = Work(title=title, sort_title=sort_title(title))
    session.add(work)
    await session.flush()

    edition = Edition(
        work_id=work.id,
        name=DEFAULT_EDITION_NAME,
        slug=DEFAULT_EDITION_SLUG,
        is_default=True,
    )
    session.add(edition)
    await session.flush()

    # Which edition was bought. The route to the work is the primary link, not
    # this column.
    entitlement.edition_id = edition.id
    session.add(
        EntitlementWork(
            entitlement_id=entitlement.id,
            work_id=work.id,
            role=WorkLinkRole.PRIMARY,
            created_by_run_id=run.id,
        )
    )
    # `platform_count` is left at its default: `derived` belongs to M4, and with
    # one platform connected a strategy tested against a constant proves nothing.
    session.add(UserWorkState(user_id=account.user_id, work_id=work.id))
    await session.flush()
    return work


def _nameless(entitlement: Entitlement) -> str:
    """A stub still needs a name when the platform sent a blank one.

    Steam has app ids — tools, depots, retired entries — whose `name` comes back
    empty, and a str is all `providers.steam` promises. Left alone the grid gets
    a card with no text and no sort key, which is unusable and unfindable.

    The id is not a title, and it is not pretending to be one: it identifies the
    row well enough to rename in M3 or to match in M2, which an empty string
    does neither of. Refusing the entry instead would be worse — the user does
    own it, and one blank name would fail the whole library.
    """

    return entitlement.provider_item_id or f"entitlement {entitlement.id}"


async def _aggregate(session: AsyncSession, *, user_id: int, entitlement_ids: list[int]) -> None:
    """`sum` across the entitlements of every work this run touched (rule 5).

    Driven from the links rather than from the new stubs: an existing
    entitlement whose playtime moved changes its work's total too, and one
    entitlement can reach several works once bundles are matched.
    """

    if not entitlement_ids:
        return
    work_ids = await session.scalars(
        select(EntitlementWork.work_id)
        .where(EntitlementWork.entitlement_id.in_(entitlement_ids))
        .distinct()
    )
    for work_id in work_ids:
        await resolve_work_aggregates(session, work_id=work_id, user_id=user_id)
