import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_account, make_provider, make_work
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import queries
from ludarium.db import Database
from ludarium.enrichment import (
    EnrichmentError,
    EnrichmentInProgressError,
    EnrichmentRun,
    enrich,
)
from ludarium.enums import EntityType, SourceKind, SyncStatus, SyncTrigger
from ludarium.models import FetchCache, FieldProvenance, Provider, SyncRun
from ludarium.models.cache import Payload
from ludarium.models.types import utcnow
from ludarium.providers import ProviderUnavailableError
from ludarium.resolver import record
from ludarium.sync import ORPHAN_AFTER

RESOURCE = "games"


class FakeProvider:
    """Answers from a fixed catalogue and remembers every batch it was asked."""

    def __init__(self, catalogue: Mapping[str, Payload], *, fail_on_call: int | None = None):
        self.catalogue = dict(catalogue)
        self.batches: list[list[str]] = []
        self._fail_on_call = fail_on_call

    @property
    def asked(self) -> list[str]:
        return [key for batch in self.batches for key in batch]

    async def __call__(self, keys: Sequence[str]) -> Mapping[str, Payload]:
        self.batches.append(list(keys))
        if len(self.batches) == self._fail_on_call:
            raise ProviderUnavailableError("igdb answered 503 for /games")
        return {key: self.catalogue[key] for key in keys if key in self.catalogue}


CATALOGUE: dict[str, Payload] = {
    "1942": {"id": 1942, "name": "The Witcher 3: Wild Hunt"},
    "1020": {"id": 1020, "name": "Grand Theft Auto V"},
    "72": [{"id": 72, "name": "Portal 2"}],
}


@pytest.fixture
async def igdb(session: AsyncSession) -> Provider:
    provider = await make_provider(session, key="igdb")
    await session.commit()
    return provider


def fetching(
    provider: FakeProvider,
    keys: Sequence[str],
    *,
    batch_size: int = 500,
    max_age: timedelta | None = None,
    into: dict[str, Payload | None] | None = None,
) -> "StepFor":
    return StepFor(provider, keys, batch_size=batch_size, max_age=max_age, into=into)


class StepFor:
    def __init__(
        self,
        provider: FakeProvider,
        keys: Sequence[str],
        *,
        batch_size: int,
        max_age: timedelta | None,
        into: dict[str, Payload | None] | None,
    ) -> None:
        self._provider = provider
        self._keys = keys
        self._batch_size = batch_size
        self._max_age = max_age
        self._into = into

    async def __call__(self, run: EnrichmentRun) -> None:
        answer = await run.fetch(
            RESOURCE,
            self._keys,
            fetch=self._provider,
            batch_size=self._batch_size,
            max_age=self._max_age,
        )
        if self._into is not None:
            self._into |= answer


async def cached(db: Database) -> dict[str, Payload | None]:
    async with db.session_factory() as reader:
        rows = await reader.execute(select(FetchCache.key, FetchCache.payload))
        return {key: payload for key, payload in rows}


async def test_every_key_comes_back_with_what_the_provider_holds_under_it(
    db: Database, igdb: Provider
) -> None:
    provider = FakeProvider(CATALOGUE)
    answer: dict[str, Payload | None] = {}

    run = await enrich(
        db, provider="igdb", step=fetching(provider, ["1942", "72", "999999"], into=answer)
    )

    assert run.status is SyncStatus.SUCCESS
    assert answer == {"1942": CATALOGUE["1942"], "72": CATALOGUE["72"], "999999": None}


async def test_the_provider_is_asked_a_batch_at_a_time(db: Database, igdb: Provider) -> None:
    provider = FakeProvider(CATALOGUE)

    await enrich(db, provider="igdb", step=fetching(provider, list("abcde"), batch_size=2))

    assert provider.batches == [["a", "b"], ["c", "d"], ["e"]]


async def test_a_second_run_over_the_same_library_asks_nothing(
    db: Database, igdb: Provider
) -> None:
    """The issue's first line: a library enriched on every sync must not burn the limit."""

    provider = FakeProvider(CATALOGUE)
    keys = ["1942", "1020", "72"]
    answer: dict[str, Payload | None] = {}

    await enrich(db, provider="igdb", step=fetching(provider, keys))
    second = await enrich(db, provider="igdb", step=fetching(provider, keys, into=answer))

    assert provider.batches == [keys]
    assert answer == {key: CATALOGUE[key] for key in keys}
    assert (second.items_seen, second.items_updated) == (3, 0)


async def test_a_key_the_provider_does_not_know_is_not_asked_about_again(
    db: Database, igdb: Provider
) -> None:
    """A playtest IGDB has never heard of stays unheard of, without a request per run."""

    provider = FakeProvider(CATALOGUE)

    await enrich(db, provider="igdb", step=fetching(provider, ["999999"]))
    await enrich(db, provider="igdb", step=fetching(provider, ["999999"]))

    assert provider.asked == ["999999"]
    assert await cached(db) == {"999999": None}


async def test_absence_is_stored_as_sql_null_rather_than_json_null(
    db: Database, igdb: Provider
) -> None:
    await enrich(db, provider="igdb", step=fetching(FakeProvider({}), ["999999"]))

    async with db.session_factory() as reader:
        nulls = await reader.scalar(text("SELECT count(*) FROM fetch_cache WHERE payload IS NULL"))

    assert nulls == 1


async def test_an_entry_older_than_max_age_is_fetched_again(db: Database, igdb: Provider) -> None:
    provider = FakeProvider(CATALOGUE)
    await enrich(db, provider="igdb", step=fetching(provider, ["1942", "1020"]))
    async with db.session_factory() as writer:
        await writer.execute(
            update(FetchCache)
            .where(FetchCache.key == "1942")
            .values(fetched_at=utcnow() - timedelta(days=31))
        )
        await writer.commit()
    provider.catalogue["1942"] = {"id": 1942, "name": "The Witcher 3: Complete Edition"}

    await enrich(
        db,
        provider="igdb",
        step=fetching(provider, ["1942", "1020"], max_age=timedelta(days=30)),
    )

    assert provider.batches[1:] == [["1942"]]
    assert (await cached(db))["1942"] == provider.catalogue["1942"]


async def test_without_max_age_an_entry_never_goes_stale(db: Database, igdb: Provider) -> None:
    provider = FakeProvider(CATALOGUE)
    await enrich(db, provider="igdb", step=fetching(provider, ["1942"]))
    async with db.session_factory() as writer:
        await writer.execute(update(FetchCache).values(fetched_at=utcnow() - timedelta(days=3650)))
        await writer.commit()

    await enrich(db, provider="igdb", step=fetching(provider, ["1942"]))

    assert provider.asked == ["1942"]


async def test_a_refetched_entry_is_updated_in_place(db: Database, igdb: Provider) -> None:
    provider = FakeProvider(CATALOGUE)
    for _ in range(2):
        await enrich(db, provider="igdb", step=fetching(provider, ["1942"], max_age=timedelta(0)))

    async with db.session_factory() as reader:
        rows = list(await reader.scalars(select(FetchCache)))

    assert len(provider.batches) == 2
    assert len(rows) == 1


async def test_a_key_named_twice_is_asked_about_once(db: Database, igdb: Provider) -> None:
    provider = FakeProvider(CATALOGUE)

    run = await enrich(db, provider="igdb", step=fetching(provider, ["1942", "72", "1942"]))

    assert provider.batches == [["1942", "72"]]
    assert run.items_seen == 2


async def test_providers_and_resources_are_cached_apart(
    db: Database, session: AsyncSession, igdb: Provider
) -> None:
    """Steam appid 72 and IGDB game 72 are different things that share a spelling."""

    await make_provider(session, key="rawg")
    await session.commit()
    provider = FakeProvider(CATALOGUE)

    async def two_resources(run: EnrichmentRun) -> None:
        await run.fetch("games", ["72"], fetch=provider, batch_size=10)
        await run.fetch("external_games/steam", ["72"], fetch=provider, batch_size=10)

    await enrich(db, provider="igdb", step=two_resources)
    await enrich(db, provider="rawg", step=fetching(provider, ["72"]))

    assert provider.batches == [["72"], ["72"], ["72"]]


async def test_overlapping_fetches_in_one_run_store_each_key_once(
    db: Database, igdb: Provider
) -> None:
    """Both look the keys up before either stores them, and neither fails.

    The second store waits at `BEGIN IMMEDIATE` until the first commits, and
    then finds the rows to update rather than inserting beside them
    (ADR-0017). What overlap does cost is the request: the provider is asked
    twice, because nothing deduplicates keys that are still in flight.
    """

    asked: list[list[str]] = []

    async def slow(keys: Sequence[str]) -> Mapping[str, Payload]:
        asked.append(list(keys))
        # Long enough that both calls are past the cache lookup before either stores.
        await asyncio.sleep(0.05)
        return {key: {"id": key} for key in keys}

    async def overlapping(run: EnrichmentRun) -> None:
        await asyncio.gather(
            *(run.fetch(RESOURCE, ["1", "2", "3"], fetch=slow, batch_size=10) for _ in range(2))
        )

    run = await enrich(db, provider="igdb", step=overlapping)

    assert run.status is SyncStatus.SUCCESS
    assert asked == [["1", "2", "3"], ["1", "2", "3"]]
    assert await cached(db) == {key: {"id": key} for key in ("1", "2", "3")}


async def test_a_failed_batch_keeps_the_batches_before_it(db: Database, igdb: Provider) -> None:
    """Nothing enrichment does removes anything, so partial progress is simply progress."""

    keys = ["1942", "1020", "72"]
    failing = FakeProvider(CATALOGUE, fail_on_call=2)

    run = await enrich(db, provider="igdb", step=fetching(failing, keys, batch_size=1))
    recovered = FakeProvider(CATALOGUE)
    await enrich(db, provider="igdb", step=fetching(recovered, keys, batch_size=1))

    assert run.status is SyncStatus.FAILED
    assert (run.items_seen, run.items_updated) == (3, 1)
    assert recovered.batches == [["1020"], ["72"]]


async def test_an_outage_is_a_failed_run_and_the_providers_own_health(
    db: Database, igdb: Provider
) -> None:
    worked = await enrich(db, provider="igdb", step=fetching(FakeProvider(CATALOGUE), ["1942"]))

    run = await enrich(
        db, provider="igdb", step=fetching(FakeProvider(CATALOGUE, fail_on_call=1), ["1020"])
    )

    async with db.session_factory() as reader:
        reporter = await reader.get_one(Provider, igdb.id)
    assert run.status is SyncStatus.FAILED
    assert run.error_text == "igdb answered 503 for /games"
    assert reporter.status is SyncStatus.FAILED
    assert reporter.last_error == "igdb answered 503 for /games"
    # "Worked this morning" survives a failure this afternoon.
    assert reporter.last_success_at == worked.finished_at


async def test_a_success_records_when_the_provider_last_worked(
    db: Database, igdb: Provider
) -> None:
    run = await enrich(db, provider="igdb", step=fetching(FakeProvider(CATALOGUE), ["1942"]))

    async with db.session_factory() as reader:
        reporter = await reader.get_one(Provider, igdb.id)
    assert run.finished_at is not None
    assert reporter.status is SyncStatus.SUCCESS
    assert reporter.last_success_at == run.finished_at
    assert reporter.last_error is None


async def test_the_run_is_the_provider_s_with_no_account(db: Database, igdb: Provider) -> None:
    run = await enrich(
        db,
        provider="igdb",
        step=fetching(FakeProvider(CATALOGUE), ["1942"]),
        trigger=SyncTrigger.SCHEDULED,
    )

    assert (run.provider_id, run.account_id, run.trigger) == (igdb.id, None, SyncTrigger.SCHEDULED)


async def test_a_bug_in_the_step_closes_the_run_and_is_let_out(
    db: Database, igdb: Provider
) -> None:
    async def broken(run: EnrichmentRun) -> None:
        raise KeyError("name")

    with pytest.raises(KeyError):
        await enrich(db, provider="igdb", step=broken)

    async with db.session_factory() as reader:
        run = (await reader.scalars(select(SyncRun))).one()
    assert run.status is SyncStatus.FAILED
    assert run.error_text == "KeyError"


async def test_a_cancelled_run_is_closed_too(db: Database, igdb: Provider) -> None:
    started = asyncio.Event()

    async def hangs(run: EnrichmentRun) -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(enrich(db, provider="igdb", step=hangs))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with db.session_factory() as reader:
        run = (await reader.scalars(select(SyncRun))).one()
    assert run.status is SyncStatus.FAILED
    assert run.error_text == "CancelledError"


async def test_a_fetch_that_answers_keys_it_was_not_asked_is_refused(
    db: Database, igdb: Provider
) -> None:
    async def generous(keys: Sequence[str]) -> Mapping[str, Payload]:
        return {"1942": CATALOGUE["1942"], "1020": CATALOGUE["1020"]}

    async def step(run: EnrichmentRun) -> None:
        await run.fetch(RESOURCE, ["1942"], fetch=generous, batch_size=10)

    with pytest.raises(EnrichmentError, match="not asked for: \\['1020'\\]"):
        await enrich(db, provider="igdb", step=step)

    assert await cached(db) == {}


async def test_a_batch_of_nothing_is_refused(db: Database, igdb: Provider) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        await enrich(db, provider="igdb", step=fetching(FakeProvider({}), ["1"], batch_size=0))


async def test_no_write_lock_is_held_while_the_provider_is_asked(
    db: Database, igdb: Provider
) -> None:
    """Runs outside the sync transaction, and outside any other.

    A sync committing while IGDB takes its time must not wait for it. If the
    pipeline held a writing transaction across the request, this commit would
    queue behind it until `busy_timeout` and then fail.
    """

    async def slow(keys: Sequence[str]) -> Mapping[str, Payload]:
        async with db.writing_session_factory() as other:
            other.add(
                FetchCache(provider_id=igdb.id, resource="other", key="1", fetched_at=utcnow())
            )
            await asyncio.wait_for(other.commit(), timeout=1)
        return {}

    async def step(run: EnrichmentRun) -> None:
        await run.fetch(RESOURCE, ["1942"], fetch=slow, batch_size=10)

    run = await enrich(db, provider="igdb", step=step)

    assert run.status is SyncStatus.SUCCESS


async def test_a_second_run_of_one_provider_is_refused_while_the_first_is_open(
    db: Database, session: AsyncSession, igdb: Provider
) -> None:
    await make_provider(session, key="rawg")
    await session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    async def holds(run: EnrichmentRun) -> None:
        started.set()
        await release.wait()

    first = asyncio.create_task(enrich(db, provider="igdb", step=holds))
    await started.wait()
    try:
        with pytest.raises(EnrichmentInProgressError):
            await enrich(db, provider="igdb", step=fetching(FakeProvider({}), ["1"]))
        # Rule 4: IGDB being busy is not RAWG's problem.
        other = await enrich(db, provider="rawg", step=fetching(FakeProvider({}), ["1"]))
        assert other.status is SyncStatus.SUCCESS
    finally:
        release.set()
    assert (await first).status is SyncStatus.SUCCESS


async def test_the_database_refuses_two_open_runs_of_one_provider(
    session: AsyncSession, igdb: Provider
) -> None:
    """The index `_open` relies on where two writers can both read the provider as idle."""

    for _ in range(2):
        session.add(SyncRun(provider_id=igdb.id, account_id=None, trigger=SyncTrigger.MANUAL))

    with pytest.raises(IntegrityError, match=r"sync_run\.provider_id"):
        await session.commit()


async def test_an_abandoned_run_stops_blocking_the_provider(
    db: Database, session: AsyncSession, igdb: Provider
) -> None:
    orphan = SyncRun(
        provider_id=igdb.id,
        account_id=None,
        trigger=SyncTrigger.MANUAL,
        started_at=utcnow() - ORPHAN_AFTER - timedelta(minutes=1),
    )
    session.add(orphan)
    await session.commit()

    run = await enrich(db, provider="igdb", step=fetching(FakeProvider({}), ["1"]))

    await session.refresh(orphan)
    assert run.status is SyncStatus.SUCCESS
    assert orphan.status is SyncStatus.FAILED
    assert orphan.error_text == "abandoned; no process was left to finish it"


async def test_only_the_provider_s_own_unattached_orphans_are_reclaimed(
    db: Database, session: AsyncSession, igdb: Provider
) -> None:
    """An abandoned sync of an account, or another provider's run, is not this run's to close."""

    rawg = await make_provider(session, key="rawg")
    account = await make_account(session, key="igdb")
    long_ago = utcnow() - ORPHAN_AFTER - timedelta(minutes=1)
    others = [
        SyncRun(
            provider_id=rawg.id, account_id=None, trigger=SyncTrigger.MANUAL, started_at=long_ago
        ),
        SyncRun(
            provider_id=igdb.id,
            account_id=account.id,
            trigger=SyncTrigger.MANUAL,
            started_at=long_ago,
        ),
    ]
    session.add_all(others)
    await session.commit()

    await enrich(db, provider="igdb", step=fetching(FakeProvider({}), ["1"]))

    for run in others:
        await session.refresh(run)
    assert [run.status for run in others] == [SyncStatus.RUNNING, SyncStatus.RUNNING]


async def test_a_recent_open_run_is_not_taken_for_an_orphan(
    db: Database, session: AsyncSession, igdb: Provider
) -> None:
    session.add(
        SyncRun(
            provider_id=igdb.id,
            account_id=None,
            trigger=SyncTrigger.MANUAL,
            started_at=utcnow() - ORPHAN_AFTER + timedelta(minutes=1),
        )
    )
    await session.commit()

    with pytest.raises(EnrichmentInProgressError):
        await enrich(db, provider="igdb", step=fetching(FakeProvider({}), ["1"]))


async def test_a_provider_nothing_seeded_refuses_to_start(db: Database) -> None:
    with pytest.raises(EnrichmentError, match="no provider row for `igdb`"):
        await enrich(db, provider="igdb", step=fetching(FakeProvider({}), ["1"]))


async def test_a_step_records_provenance_under_its_run(
    db: Database, session: AsyncSession, igdb: Provider
) -> None:
    """Rule 9 from inside a step: the run id is a real `sync_run` for the rows to name."""

    work = await make_work(session)
    await session.commit()

    async def step(run: EnrichmentRun) -> None:
        answer = await run.fetch(RESOURCE, ["1942"], fetch=FakeProvider(CATALOGUE), batch_size=1)
        payload = answer["1942"]
        assert isinstance(payload, dict)
        async with db.writing_session_factory() as writer:
            await record(
                writer,
                entity_type=EntityType.WORK,
                entity_id=work.id,
                field="summary",
                source_kind=SourceKind.METADATA_PROVIDER,
                source_ref="igdb",
                value=payload["name"],
                run_id=run.id,
            )
            await writer.commit()

    run = await enrich(db, provider="igdb", step=step)

    row = (await session.scalars(select(FieldProvenance))).one()
    assert row.run_id == run.id


async def test_more_keys_than_one_in_list_holds_are_read_and_written_in_batches(
    db: Database, igdb: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library past the driver's bind ceiling is more statements, not an error."""

    monkeypatch.setattr(queries, "BIND_LIMIT", 2)
    widest: list[int] = []

    def measure(
        _connection: object, _cursor: object, statement: str, parameters: Any, *_: object
    ) -> None:
        if statement.startswith("SELECT") and "fetch_cache" in statement:
            widest.append(len(parameters))

    event.listen(db.engine.sync_engine, "before_cursor_execute", measure)
    keys = [str(number) for number in range(5)]
    provider = FakeProvider({key: {"id": int(key)} for key in keys})

    await enrich(db, provider="igdb", step=fetching(provider, keys, max_age=timedelta(0)))
    await enrich(db, provider="igdb", step=fetching(provider, keys))

    assert provider.batches == [keys]
    assert len(await cached(db)) == 5
    # Two keys, plus the provider and the resource every lookup also binds.
    assert max(widest) <= 4
