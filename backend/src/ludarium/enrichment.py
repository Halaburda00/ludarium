"""Enrichment: asking a provider about what the library holds, once, and outside any sync.

The substrate every M2a fetch runs on — IGDB metadata, RAWG scores, covers,
store data for `ItemKind` — so that there is one cache and one run history
rather than one per caller (ADR-0019). A caller is a *step*: a function handed
an `EnrichmentRun`, which asks through `EnrichmentRun.fetch` and records what it
learns as provenance, like any provider (rule 9).

The transaction boundaries are the opposite of a sync's, deliberately. A sync
commits everything with its status, because a partial library could mark
things removed (rule 1). Enrichment removes nothing, so each batch commits as
it arrives: a run that fails on its tenth batch keeps nine, and the next run
asks only for the tenth. No transaction is open while a provider is being
asked, so an outage holds no write lock and fails no sync (rule 4).

Rate limits are not enforced here. They belong to each client, where no caller
can exceed them by existing (#45), and they differ in shape — IGDB's is per
second, Steam's store is undocumented.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from ludarium.db import Database
from ludarium.enums import SyncStatus, SyncTrigger
from ludarium.models import FetchCache, Provider, SyncRun
from ludarium.models.cache import Payload
from ludarium.models.types import utcnow
from ludarium.providers import ProviderError
from ludarium.queries import in_batches
from ludarium.sync import reclaim_orphans

# Every key the batch was asked about that the provider knows, and nothing else.
# A key left out is recorded as absent, so a fetch that truncates its answer —
# IGDB's `limit` caps rows, not keys — has to page rather than return short.
type BatchFetch = Callable[[Sequence[str]], Awaitable[Mapping[str, Payload]]]
type Step = Callable[["EnrichmentRun"], Awaitable[None]]


class EnrichmentError(Exception):
    """The run could not start, or a step broke the contract it runs under."""


class EnrichmentInProgressError(EnrichmentError):
    """This provider already has an open enrichment run.

    Raised rather than queued, as a sync is: the second trigger is a sync
    finishing while the last one's enrichment is still going, and the open run
    will see whatever that sync added once the next one starts.
    """


@dataclass
class _Progress:
    """Counted outside every session, so a failed run still says how far it got."""

    asked: int = 0
    fetched: int = 0


class EnrichmentRun:
    """One open run: its id for the provenance rows a step writes, and a cached fetch."""

    def __init__(
        self, database: Database, *, run_id: int, provider_id: int, progress: _Progress
    ) -> None:
        self._database = database
        self.id = run_id
        self.provider_id = provider_id
        self._progress = progress

    async def fetch(
        self,
        resource: str,
        keys: Iterable[str],
        *,
        fetch: BatchFetch,
        batch_size: int,
        max_age: timedelta | None = None,
    ) -> dict[str, Payload | None]:
        """Every key's payload, from the cache where it is fresh and from `fetch` where not.

        None for a key the provider has nothing under. `max_age` None means a
        cached answer never goes stale; a caller that knows its provider revises
        records passes how long it trusts one.

        The provider is asked `batch_size` keys at a time, and each batch is
        committed before the next is asked for.
        """

        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, not {batch_size}")
        wanted = list(dict.fromkeys(keys))
        self._progress.asked += len(wanted)

        found = await self._cached(resource, wanted, max_age)
        missing = [key for key in wanted if key not in found]
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            answer = await fetch(batch)
            strays = sorted(set(answer) - set(batch))
            if strays:
                # Stored, these would be cached under keys nobody asked about,
                # which is a bug in the step rather than a fact about the provider.
                raise EnrichmentError(
                    f"a fetch for {resource} answered keys it was not asked for: {strays[:5]}"
                )
            records = {key: answer.get(key) for key in batch}
            await self._store(resource, records)
            self._progress.fetched += len(batch)
            found |= records
        return {key: found[key] for key in wanted}

    async def _cached(
        self, resource: str, keys: Sequence[str], max_age: timedelta | None
    ) -> dict[str, Payload | None]:
        oldest = None if max_age is None else utcnow() - max_age
        found: dict[str, Payload | None] = {}
        async with self._database.reading_session_factory() as session:
            for batch in in_batches(keys):
                rows = await session.execute(
                    select(FetchCache.key, FetchCache.payload, FetchCache.fetched_at).where(
                        FetchCache.provider_id == self.provider_id,
                        FetchCache.resource == resource,
                        FetchCache.key.in_(batch),
                    )
                )
                for key, payload, fetched_at in rows:
                    if oldest is None or fetched_at >= oldest:
                        found[key] = payload
        return found

    async def _store(self, resource: str, records: Mapping[str, Payload | None]) -> None:
        # One open run per provider means nothing else writes these keys. The
        # IMMEDIATE transaction and the unique constraint are the backstop if
        # that ever stops being true (ADR-0017).
        moment = utcnow()
        async with self._database.writing_session_factory() as session:
            existing: dict[str, FetchCache] = {}
            for batch in in_batches(list(records)):
                for cached in await session.scalars(
                    select(FetchCache).where(
                        FetchCache.provider_id == self.provider_id,
                        FetchCache.resource == resource,
                        FetchCache.key.in_(batch),
                    )
                ):
                    existing[cached.key] = cached
            for key, payload in records.items():
                row = existing.get(key)
                if row is None:
                    row = FetchCache(provider_id=self.provider_id, resource=resource, key=key)
                    session.add(row)
                row.payload = payload
                row.fetched_at = moment
            await session.commit()


async def enrich(
    database: Database,
    *,
    provider: str,
    step: Step,
    trigger: SyncTrigger = SyncTrigger.MANUAL,
) -> SyncRun:
    """Run one step as one `sync_run` of `provider` and return the run, finished either way.

    The row a sync would write, with no account: `account_id` is null for runs
    that sync none, which `docs/schema.md` reserved for exactly this. A
    `ProviderError` is a status rather than an exception, so a sync that
    triggers enrichment when it finishes cannot be failed by it (rule 4).
    Anything else is a bug, closed as a failed run and let out.
    """

    run = await _open(database, provider=provider, trigger=trigger)
    progress = _Progress()
    try:
        await step(
            EnrichmentRun(database, run_id=run.id, provider_id=run.provider_id, progress=progress)
        )
    except ProviderError as exc:
        # Contractually free of credentials (rule 7), as in `sync_account`.
        return await _close(database, run, progress, SyncStatus.FAILED, error=str(exc))
    except BaseException as exc:
        # `BaseException` so a cancelled run is closed too, for the reason
        # `sync_account` gives: `CancelledError` is not an `Exception`.
        await _close(database, run, progress, SyncStatus.FAILED, error=type(exc).__name__)
        raise
    return await _close(database, run, progress, SyncStatus.SUCCESS)


async def _open(database: Database, *, provider: str, trigger: SyncTrigger) -> SyncRun:
    """Claim the provider. Check-then-act, which the IMMEDIATE transaction makes safe.

    On SQLite a second writer waits at `BEGIN` and then finds the first run; on
    an engine where both can read the provider as idle, the partial unique index
    refuses the second insert and its `IntegrityError` goes out uncaught
    (ADR-0017).
    """

    async with database.writing_session_factory() as session:
        reporter = await session.scalar(select(Provider).where(Provider.key == provider))
        if reporter is None:
            raise EnrichmentError(f"no provider row for `{provider}`; the seed is out of step")
        unattached = (SyncRun.provider_id == reporter.id, SyncRun.account_id.is_(None))
        await reclaim_orphans(session, *unattached)
        open_run = select(SyncRun.id).where(*unattached, SyncRun.status == SyncStatus.RUNNING)
        if await session.scalar(open_run) is not None:
            raise EnrichmentInProgressError(f"`{provider}` is already enriching")
        run = SyncRun(provider_id=reporter.id, account_id=None, trigger=trigger)
        session.add(run)
        await session.commit()
        return run


async def _close(
    database: Database,
    run: SyncRun,
    progress: _Progress,
    status: SyncStatus,
    *,
    error: str | None = None,
) -> SyncRun:
    """Finish the run and report the provider's health, in one transaction.

    Nothing is rolled back first, unlike a sync: every batch the run fetched was
    committed when it arrived, and keeping it is the point.

    `items_seen` is every distinct key asked about and `items_updated` the ones
    the provider was actually asked for, so a second run over an unchanged
    library reads as many seen and none updated — the "never re-fetch" rule as
    a number in the status panel.
    """

    moment = utcnow()
    async with database.writing_session_factory() as session:
        closed = await session.get_one(SyncRun, run.id)
        reporter = await session.get_one(Provider, run.provider_id)
        closed.status = status
        closed.finished_at = moment
        closed.error_text = error
        closed.items_seen = progress.asked
        closed.items_updated = progress.fetched
        reporter.status = status
        reporter.last_error = error
        if status is SyncStatus.SUCCESS:
            reporter.last_success_at = moment
        await session.commit()
        return closed
