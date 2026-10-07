import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import create_schema, make_account
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_sync import THREE_GAMES, FakeLibrary

from ludarium import schedule as schedule_module
from ludarium.config import ConfigurationError, Settings
from ludarium.db import Database
from ludarium.enrichment import Step
from ludarium.enums import ProviderKind, SourceKind, SyncStatus, SyncTrigger
from ludarium.main import create_app
from ludarium.models import Account, Provider, SyncRun
from ludarium.providers.base import FetchedLibrary
from ludarium.schedule import STAGGER, SyncSchedule
from ludarium.steps import Scheduled, StepContext

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
async def context(db: Database, settings: Settings) -> AsyncIterator[StepContext]:
    async with httpx.AsyncClient() as client:
        yield StepContext(client, db, settings)


def schedule_for(context: StepContext, **settings: Any) -> SyncSchedule:
    changed = context.settings.model_copy(update=settings)
    return SyncSchedule(StepContext(context.http, context.database, changed), Scheduled())


def provider(key: str, *, last_success_at: datetime | None = None) -> Provider:
    return Provider(
        key=key,
        kind=ProviderKind.PLATFORM,
        source_kind=SourceKind.PLATFORM_API,
        display_name=key.title(),
        last_success_at=last_success_at,
    )


@pytest.fixture
def libraries(monkeypatch: pytest.MonkeyPatch) -> dict[int, Any]:
    """The library each account answers with, by account id; a game list by default."""

    answers: dict[int, Any] = {}

    def library_for(account: Account, *, key: str, client: httpx.AsyncClient) -> Any:
        return answers.get(account.id, FakeLibrary(THREE_GAMES, key=key))

    monkeypatch.setattr(schedule_module, "library_for", library_for)
    return answers


@pytest.fixture
def enriched(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The steps each job ran after its syncs, by provider key."""

    calls: list[list[str]] = []

    async def enrich_after_sync(
        context: StepContext,
        planned: Sequence[tuple[str, Step]],
        *,
        trigger: SyncTrigger,
        scheduled: Scheduled,
    ) -> None:
        assert trigger is SyncTrigger.SCHEDULED
        calls.append([key for key, _ in planned])
        for key, _ in planned:
            scheduled.done(key)

    monkeypatch.setattr(schedule_module, "enrich_after_sync", enrich_after_sync)
    return calls


def test_every_platform_with_a_client_syncs_every_six_hours_by_default(
    context: StepContext,
) -> None:
    intervals = schedule_for(context).intervals(["steam", "epic", "igdb", "manual"])

    assert intervals == {"steam": timedelta(hours=6), "epic": timedelta(hours=6)}


def test_a_platform_can_be_given_its_own_interval_or_none(context: StepContext) -> None:
    schedule = schedule_for(context, sync_interval_hours=12, sync_intervals={"epic": 0})

    assert schedule.intervals(["steam", "epic"]) == {"steam": timedelta(hours=12)}


@pytest.mark.parametrize("key", ["igdb", "manual", "gogg"])
def test_an_interval_for_something_that_cannot_sync_is_refused(
    context: StepContext, key: str
) -> None:
    with pytest.raises(ConfigurationError, match=key):
        schedule_for(context, sync_intervals={key: 1}).intervals(["steam", "igdb", "manual"])


async def test_a_platform_runs_an_interval_after_its_last_success(context: StepContext) -> None:
    schedule = schedule_for(context)
    schedule.start(
        [
            # Synced an hour ago: next due in five.
            provider("steam", last_success_at=NOW - timedelta(hours=1)),
            # Never synced, and overdue: soon, but not together.
            provider("epic"),
        ],
        now=NOW,
    )
    try:
        assert schedule.next_runs() == {
            "epic": NOW + STAGGER,
            "steam": NOW + timedelta(hours=5),
        }
    finally:
        await schedule.stop()


async def test_a_restart_that_finds_every_platform_due_staggers_them(context: StepContext) -> None:
    schedule = schedule_for(context)
    long_ago = NOW - timedelta(days=3)
    schedule.start(
        [provider("steam", last_success_at=long_ago), provider("epic", last_success_at=long_ago)],
        now=NOW,
    )
    try:
        assert sorted(schedule.next_runs().values()) == [NOW + STAGGER, NOW + 2 * STAGGER]
    finally:
        await schedule.stop()


async def test_a_job_syncs_every_active_connected_account_then_enriches(
    context: StepContext,
    session: AsyncSession,
    libraries: dict[int, Any],
    enriched: list[list[str]],
) -> None:
    first = await make_account(session, external_account_id="1")
    second = await make_account(session, external_account_id="2")
    idle = await make_account(session, external_account_id="3")
    idle.is_active = False
    imported = await make_account(session, external_account_id="4")
    imported.is_derived = True
    await session.commit()

    runs = await schedule_for(context).sync_provider("steam")

    assert [(run.account_id, run.trigger, run.status) for run in runs] == [
        (first.id, SyncTrigger.SCHEDULED, SyncStatus.SUCCESS),
        (second.id, SyncTrigger.SCHEDULED, SyncStatus.SUCCESS),
    ]
    assert enriched == [["steam_store"]]


async def test_an_account_already_syncing_is_skipped_and_the_rest_go_on(
    context: StepContext,
    session: AsyncSession,
    libraries: dict[int, Any],
    enriched: list[list[str]],
) -> None:
    busy = await make_account(session, external_account_id="1")
    free = await make_account(session, external_account_id="2")
    session.add(
        SyncRun(
            provider_id=busy.provider_id,
            account_id=busy.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
        )
    )
    await session.commit()

    runs = await schedule_for(context).sync_provider("steam")

    assert [run.account_id for run in runs] == [free.id]


async def test_nothing_is_enriched_when_every_account_failed(
    context: StepContext,
    session: AsyncSession,
    libraries: dict[int, Any],
    enriched: list[list[str]],
) -> None:
    from ludarium.providers.base import ProviderUnavailableError

    account = await make_account(session)
    await session.commit()
    libraries[account.id] = FakeLibrary(error=ProviderUnavailableError("Steam is down"))

    runs = await schedule_for(context).sync_provider("steam")

    assert [run.status for run in runs] == [SyncStatus.FAILED]
    assert enriched == []


async def test_a_disabled_platform_is_not_synced(
    context: StepContext, session: AsyncSession, libraries: dict[int, Any]
) -> None:
    account = await make_account(session)
    platform = await session.get_one(Provider, account.provider_id)
    platform.enabled = False
    await session.commit()

    assert await schedule_for(context).sync_provider("steam") == []


class Hanging(FakeLibrary):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def fetch_library(self) -> FetchedLibrary:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("never answered")  # pragma: no cover


async def test_stopping_closes_a_run_in_flight_as_failed(
    context: StepContext, session: AsyncSession, libraries: dict[int, Any]
) -> None:
    account = await make_account(session)
    await session.commit()
    hanging = Hanging()
    libraries[account.id] = hanging
    schedule = schedule_for(context)
    schedule.start([], now=NOW)
    job = asyncio.create_task(schedule.sync_provider("steam"))
    await hanging.started.wait()

    await schedule.stop()

    assert job.cancelled()
    run = await session.scalar(select(SyncRun))
    assert run is not None
    assert (run.status, run.error_text) == (SyncStatus.FAILED, "CancelledError")
    await session.commit()


def test_the_app_schedules_its_platforms_and_a_demo_does_not(settings: Settings) -> None:
    create_schema(settings.database_url)

    with TestClient(create_app(settings)) as client:
        assert set(client.app.state.schedule.next_runs()) == {"steam", "epic"}  # type: ignore[attr-defined]
    with TestClient(create_app(settings.model_copy(update={"demo": True}))) as client:
        assert client.app.state.schedule is None  # type: ignore[attr-defined]


def test_an_interval_for_an_unknown_platform_stops_the_start(settings: Settings) -> None:
    create_schema(settings.database_url)
    broken = settings.model_copy(update={"sync_intervals": {"gogg": 6}})

    with pytest.raises(ConfigurationError, match="gogg"), TestClient(create_app(broken)):
        pass  # pragma: no cover
