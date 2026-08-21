"""One run: ask a provider what an account owns and land it without losing anything.

The transaction boundaries are the design. The `sync_run` row is committed
before the fetch, so a process killed mid-run leaves a `running` row to explain
itself rather than no evidence at all; everything the run then changes lands in
a second transaction that commits together with `status = success`. There is no
moment where a partial result is committed and still looks like a finished sync,
which is what rule 1 needs from the write side.

That boundary is also the whole of "only a `success` run may mark anything
removed". It is not a check anywhere — the sweep's updates commit with the
status or roll back with it, so no failed run can leave a removal behind.

Unlike the resolver, which leaves the transaction to its caller, this owns it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.crypto import CredentialDecryptionError, get_cipher
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
from ludarium.providers.registry import build_library
from ludarium.queries import in_batches
from ludarium.resolver import record_many, resolve, resolve_work_aggregates_many
from ludarium.titles import sort_title

# Every work has at least one edition, so a provider entry that says nothing
# about which one was bought still has something to attach to.
DEFAULT_EDITION_NAME: Final = "Standard"
DEFAULT_EDITION_SLUG: Final = "standard"


# A run measures in seconds — 9 for a 2000-game library (#23) — and the fetch
# is bounded by the client's timeout and three tenacity attempts. An hour is
# therefore not a slow sync; it is a process that was killed between opening the
# row and closing it, and the index below would otherwise let that one orphan
# block the account forever.
ORPHAN_AFTER: Final = timedelta(hours=1)


class _Unusable:
    """A library that could not be built, presented as one that cannot answer.

    Rule 4 within a single provider: several accounts on one platform are as
    independent of each other as two platforms are. A stored credential that
    will not decrypt is that account's problem, and raising it would end the
    request over accounts that were about to sync fine.

    As a `ProviderError` it becomes a failed run instead — recorded in the
    history, counted in the provider's health, and visible in the status panel,
    which is where every other failure already is.
    """

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self._reason = reason

    async def validate_credentials(self) -> None:
        raise ProviderError(self._reason)

    async def fetch_library(self) -> list[LibraryItem]:
        raise ProviderError(self._reason)


def library_for(account: Account, *, key: str, client: httpx.AsyncClient) -> LibraryProvider:
    """The client that answers for one account, or a stand-in that reports why not.

    Free of HTTP concerns on purpose: the scheduler will want exactly this
    sequence, and a function that raised `HTTPException` would leave it to
    duplicate the decrypt-and-build rather than reuse it.
    """

    if account.credentials_encrypted is None:
        return _Unusable(key, "no credential is stored for this account")
    try:
        secret = get_cipher().decrypt(account.credentials_encrypted)
    except CredentialDecryptionError as exc:
        # The message names the setting, never the ciphertext (rule 7).
        return _Unusable(key, str(exc))
    return build_library(
        key,
        external_account_id=account.external_account_id or "",
        secret=secret,
        client=client,
    )


class SyncError(Exception):
    """The run could not start. Not a provider failure — those are a run status."""


class SyncInProgressError(SyncError):
    """This account already has an open run, and one at a time is the rule.

    Raised rather than queued: the caller is a user who double-clicked, or a
    scheduler firing over a sync that has not finished, and both want to be told
    rather than to wait.
    """


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
    run = await _open(session, account=account, reporter=reporter, trigger=trigger)

    seen = _Progress()
    try:
        items = await library.fetch_library()
        seen.items_seen = len(items)
        await _apply(session, run=run, account=account, reporter=reporter, items=items)
    except ProviderError as exc:
        # Safe to store: `ProviderError` never carries a credential, which is
        # the contract `providers.base` states rather than a hope (rule 7).
        await _close(session, run, reporter, account, seen, SyncStatus.FAILED, error=str(exc))
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
        await _close(
            session, run, reporter, account, seen, SyncStatus.FAILED, error=type(exc).__name__
        )
        raise
    await _close(session, run, reporter, account, seen, SyncStatus.SUCCESS)
    return run


async def _open(
    session: AsyncSession, *, account: Account, reporter: Provider, trigger: SyncTrigger
) -> SyncRun:
    """Claim the account by inserting the run. The index decides, not a check beforehand.

    A collision is only reported as `SyncInProgressError` once an open run has
    actually been found, so a violation of some other constraint on the row is
    not relabelled into a concurrency answer and looked for in the wrong place.

    And when the lookup finds nothing, the blocker finished in the window
    between the collision and the question. Nothing is in the way any more, so
    the honest answer is the sync the caller asked for rather than an error
    about a run that no longer exists — the second attempt either succeeds or
    fails for a reason that is not this one.

    Everything past the collision is written in ids rather than instances,
    because the rollback expires every instance in the session and reading an
    expired column attribute lazy-loads, which is an error on an async session.
    The refresh below is the other half of the same rule: the caller goes on to
    use both of these.
    """

    provider_id, account_id = reporter.id, account.id
    await _reclaim(session, account_id=account_id)
    try:
        return await _insert(
            session, provider_id=provider_id, account_id=account_id, trigger=trigger
        )
    except IntegrityError as exc:
        await session.rollback()
        for instance in (account, reporter):
            await session.refresh(instance)
        if await _open_run(session, account_id) is not None:
            raise SyncInProgressError(f"account {account_id} is already syncing") from exc
    return await _insert(session, provider_id=provider_id, account_id=account_id, trigger=trigger)


async def _insert(
    session: AsyncSession, *, provider_id: int, account_id: int, trigger: SyncTrigger
) -> SyncRun:
    run = SyncRun(
        provider_id=provider_id,
        account_id=account_id,
        trigger=trigger,
        status=SyncStatus.RUNNING,
    )
    session.add(run)
    await session.commit()
    return run


async def _open_run(session: AsyncSession, account_id: int) -> SyncRun | None:
    run: SyncRun | None = await session.scalar(
        select(SyncRun).where(
            SyncRun.account_id == account_id, SyncRun.status == SyncStatus.RUNNING
        )
    )
    return run


async def _reclaim(session: AsyncSession, *, account_id: int) -> None:
    """Close an orphan so one killed process does not lock the account out for good.

    `_close` covers cancellation and every exception, so what is left is a hard
    kill between the first commit and the second — the row says `running` and
    nothing is left alive to finish it. Marked `failed`, never `success`, so it
    stays incapable of owning a removal (rule 1); whatever it had written was
    lost with its transaction anyway.

    The threshold is the residual risk in one number: a run still genuinely
    going after `ORPHAN_AFTER` would be reclaimed out from under itself and
    could then race the run that replaced it. That is what makes the number
    generous rather than tight.

    Flushed, not committed. The update rides on the insert that follows it, so a
    sync costs one transaction rather than two — and the reclaim survives
    exactly when the run it made room for does.
    """

    await session.execute(
        update(SyncRun)
        .where(
            SyncRun.account_id == account_id,
            SyncRun.status == SyncStatus.RUNNING,
            SyncRun.started_at < utcnow() - ORPHAN_AFTER,
        )
        .values(
            status=SyncStatus.FAILED,
            finished_at=utcnow(),
            error_text="abandoned; no process was left to finish it",
        )
    )
    await session.flush()


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
    reporter: Provider,
    account: Account,
    seen: _Progress,
    status: SyncStatus,
    *,
    error: str | None = None,
) -> None:
    """Finish the run and report the provider's health. Anything short of success rolls back.

    The rollback is the enforcement of rule 1 in its plainest form, and it comes
    before the status is written so the two can never disagree: there is no
    committed state in which a run reports `failed` over changes it kept, and
    none in which a failed run left a removal behind.

    Which is also why the health columns are written after it rather than
    before: they describe the run, not the library, and must survive the
    rollback that takes everything the run did.

    `items_seen` is set here for the same reason — it describes the provider's
    answer rather than anything this run wrote.
    """

    if status is not SyncStatus.SUCCESS:
        await session.rollback()
        # Expired by the rollback, and assigning to an expired column attribute
        # would load it lazily — which is an error on an async session.
        for instance in (run, reporter, account):
            await session.refresh(instance)

    moment = utcnow()
    run.items_seen = seen.items_seen
    run.status = status
    run.finished_at = moment
    run.error_text = error
    _report(reporter, account, status, error, moment)
    await session.commit()


def _report(
    reporter: Provider,
    account: Account,
    status: SyncStatus,
    error: str | None,
    moment: datetime,
) -> None:
    """Rule 4: one provider's outage stays one provider's outage.

    The reporter's row, not the account's provider — a Galaxy import is Galaxy's
    health, and the Battle.net account it writes to has never been asked
    anything itself.

    `status` and `last_error` say what the provider's *last run* did, across all
    of its accounts, because that is the only thing the columns can say: they
    sit on the provider row and there is no per-account equivalent. Two Steam
    accounts therefore overwrite each other here, and the later run wins whether
    it failed or succeeded. That is a summary going stale, not a fact being
    lost — `sync_run` keeps every attempt with its `account_id`, and
    `account.last_success_at` below is per account and never moves backwards.
    Per-account health columns arrive with the multi-account UI in M4.

    `last_success_at` only ever moves forward. A failure records that it broke
    without erasing when it last worked, which is the pair the status panel
    needs to tell "never worked" from "worked this morning".

    `last_error` takes the same string the run row got, and for the same reason
    (rule 7): a `ProviderError` message is contractually credential-free, and
    anything else has already been reduced to its type name by the caller.
    """

    reporter.status = status
    reporter.last_error = error
    if status is SyncStatus.SUCCESS:
        reporter.last_success_at = moment
        account.last_success_at = moment


async def _apply(
    session: AsyncSession,
    *,
    run: SyncRun,
    account: Account,
    reporter: Provider,
    items: list[LibraryItem],
) -> None:
    """One library, in phases rather than one item at a time.

    Every item used to cost the same fixed set of sequential round-trips — its
    own lookup, its own four reads of `field_provenance`, its own recomputed
    work total (#23). The work is the same; what changes is that each question
    is asked once for the whole library, so what is left per game is writes.

    The order within an item is what it always was, and has to be: the row
    exists before anything asserts a field about it, the fields are resolved
    before a stub copies the resolved title, and the aggregates are last because
    a removal moves a total as surely as an update does.
    """

    # `items_seen` is `_close`'s, so that a failed run keeps it.
    known = await _known(session, account=account)
    seen: list[Entitlement] = []
    fresh: list[Entitlement] = []
    for item in items:
        entitlement = known.get(item.provider_item_id)
        if entitlement is None:
            entitlement = _blank(account, item)
            session.add(entitlement)
            # Back into the map, so a provider that lists one item twice finds
            # the row it just made rather than colliding with it.
            known[item.provider_item_id] = entitlement
            fresh.append(entitlement)
        _refresh(entitlement, item)
        seen.append(entitlement)
    # One flush for the whole library, which is where every id the provenance
    # rows are about to address gets assigned. Still one INSERT per row on
    # SQLite — a generated id has to come back through `lastrowid`, one
    # execution at a time — so what this saves is the flushes around them, not
    # the inserts themselves.
    await session.flush()

    for entitlement, item in zip(seen, items, strict=True):
        await _assert_fields(
            session, run=run, reporter=reporter, entitlement=entitlement, item=item
        )

    await _stub(session, run=run, account=account, entitlements=fresh)
    # Counted rather than incremented: a counter touched inside the loop is
    # dirty at every flush in it, which was an `UPDATE sync_run` per item.
    run.items_added = len(fresh)
    run.items_updated = len(seen) - len(fresh)

    touched = [entitlement.id for entitlement in seen]
    # A removal changes its work's totals as surely as an update does, so the
    # swept rows join the list the aggregates are recomputed from.
    touched += _sweep(run=run, known=known, items=items)
    await session.flush()
    await _aggregate(session, user_id=account.user_id, entitlement_ids=touched)


async def _known(session: AsyncSession, *, account: Account) -> dict[str, Entitlement]:
    """Every row of this account the run might touch, keyed by the platform's id.

    One query where there was one per item plus one for the sweep, and both of
    those wanted almost the same set: the upsert needs removed rows too, so it
    can restore them, and the sweep wants only the live ones — which is a
    `removed_at` test in Python rather than a second trip.

    Rows with `origin = manual` are excluded (rule 2), and so are rows the
    platform cannot name: a null `provider_item_id` matches no item and is not
    something the platform stopped listing — it is a row the platform was never
    asked about. An import writes those.

    The `origin` predicate is deliberately redundant. A CHECK constraint already
    makes it impossible for a manual row to carry a `provider_item_id` at all,
    so the null test would drop it anyway; rule 2 is worth two independent
    guards, and this is the one that says so where the query is written.
    """

    rows = await session.scalars(
        select(Entitlement).where(
            Entitlement.account_id == account.id,
            Entitlement.origin != EntitlementOrigin.MANUAL,
            Entitlement.provider_item_id.is_not(None),
        )
    )
    # The key is not null by the predicate above; mypy cannot see that.
    return {str(entitlement.provider_item_id): entitlement for entitlement in rows}


def _blank(account: Account, item: LibraryItem) -> Entitlement:
    return Entitlement(
        user_id=account.user_id,
        account_id=account.id,
        origin=EntitlementOrigin.SYNC,
        provider_item_id=item.provider_item_id,
        # Seeded because the column is NOT NULL and the row has to exist before
        # a provenance row can address it. The resolver owns it from the next
        # statement onwards.
        provider_title=item.title,
    )


def _refresh(entitlement: Entitlement, item: LibraryItem) -> None:
    """What every run writes about an item it can still see, new or not."""

    _describe(entitlement, item)
    # Touched by every run that still sees the item; `first_seen_at` is not, so
    # that "owned since" survives every later run and a removal after it.
    entitlement.last_seen_at = utcnow()
    if entitlement.removed_at is not None:
        # The platform listing it again is the best evidence there is that the
        # removal was precautionary. Undoing it automatically is what makes an
        # outage cosmetic rather than a thousand-click repair — the one-click
        # restore in the removed view is for the cases where it was not
        # (ADR-0010). `first_seen_at` is untouched, so "owned since" survives
        # the round trip and reads the same as if nothing had happened.
        entitlement.removed_at = None
        entitlement.removed_by_run_id = None


def _sweep(*, run: SyncRun, known: dict[str, Entitlement], items: list[LibraryItem]) -> list[int]:
    """Mark what the provider stopped listing. Never a DELETE (rule 1).

    Absence is not proof of anything. A game drops out of a response because of
    a delisting, a region change, an expired family share — or a transient fault
    that produces a byte-identical answer. So the row stays, keeping its
    `first_seen_at`, its playtime and the user's own state, and the removed view
    offers it back in one click.

    "Only a `success` run may do this" is not a check here; it is the
    transaction. These updates commit with the status or roll back with it, so
    there is no path by which a failed run leaves a removal behind.

    An empty library sweeps everything, deliberately: that is what a platform
    saying "you own nothing" looks like, and telling it apart from a truncated
    response belongs to the provider, which is why `SteamProvider` refuses a
    body shorter than the count Steam sent with it.

    The set difference is taken in Python rather than as `NOT IN (...)`, which
    binds one parameter per owned item and fails outright past SQLite's ceiling.
    A library that large is rare, but the failure would not be: the sweep raises
    inside the run's own transaction, so every run of that account rolls back
    and reports `failed` until the library shrinks. Nothing is paid for it — the
    rows are the ones `_known` already loaded, and an already-removed row is
    left alone rather than re-stamped with this run's id.
    """

    owned = {item.provider_item_id for item in items}
    stale = [
        entitlement
        for provider_item_id, entitlement in known.items()
        if provider_item_id not in owned and entitlement.removed_at is None
    ]
    moment = utcnow()
    for entitlement in stale:
        entitlement.removed_at = moment
        entitlement.removed_by_run_id = run.id
    run.items_removed = len(stale)
    return [entitlement.id for entitlement in stale]


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

    The three fields go down together and the rows come straight back to
    `resolve`, so the run reads them once instead of four times over (#23).
    """

    values: dict[str, ScalarValue | None] = {
        "provider_item_id": item.provider_item_id,
        "provider_title": item.title,
        # None where the platform reported nothing, which the resolver reads as
        # "this source has no figure" rather than as zero minutes played.
        "playtime_minutes": item.playtime_minutes,
    }
    recorded = await record_many(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        source_kind=reporter.source_kind,
        source_ref=reporter.key,
        values=values,
        run_id=run.id,
    )
    await resolve(
        session,
        entity_type=EntityType.ENTITLEMENT,
        entity_id=entitlement.id,
        fields=list(values),
        recorded=recorded,
    )


async def _stub(
    session: AsyncSession, *, run: SyncRun, account: Account, entitlements: list[Entitlement]
) -> None:
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

    Three flushes for the whole library rather than three per game: an edition
    needs its work's id and a link needs the edition's, so the phases cannot
    merge — but nothing makes them per-item (#23). Worth about 5% of a first
    run's wall clock and no round-trips at all on SQLite, which inserts a row
    whose generated id is wanted back one statement at a time whatever the
    caller batches. An engine that can batch those has more to gain, and
    ADR-0004 keeps PostgreSQL a target.
    """

    if not entitlements:
        return

    works = []
    for entitlement in entitlements:
        title = entitlement.provider_title.strip() or _nameless(entitlement)
        work = Work(title=title, sort_title=sort_title(title))
        session.add(work)
        works.append(work)
    await session.flush()

    editions = []
    for work in works:
        edition = Edition(
            work_id=work.id,
            name=DEFAULT_EDITION_NAME,
            slug=DEFAULT_EDITION_SLUG,
            is_default=True,
        )
        session.add(edition)
        editions.append(edition)
    await session.flush()

    for entitlement, work, edition in zip(entitlements, works, editions, strict=True):
        # Which edition was bought. The route to the work is the primary link,
        # not this column.
        entitlement.edition_id = edition.id
        session.add(
            EntitlementWork(
                entitlement_id=entitlement.id,
                work_id=work.id,
                role=WorkLinkRole.PRIMARY,
                created_by_run_id=run.id,
            )
        )
        # `platform_count` is left at its default: `derived` belongs to M4, and
        # with one platform connected a strategy tested against a constant
        # proves nothing.
        session.add(UserWorkState(user_id=account.user_id, work_id=work.id))
    await session.flush()


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

    Batched, and the ids are named a bind-limit at a time: a first sync touches
    every game in the library, and one `IN (...)` that long is the same cliff
    the sweep is written to avoid.
    """

    unique = list(dict.fromkeys(entitlement_ids))
    work_ids: list[int] = []
    for batch in in_batches(unique):
        work_ids += await session.scalars(
            select(EntitlementWork.work_id)
            .where(EntitlementWork.entitlement_id.in_(batch))
            .distinct()
        )
    await resolve_work_aggregates_many(session, work_ids=work_ids, user_id=user_id)
