"""Syncing every platform's accounts on a timer, without anyone asking (#127, ADR-0036).

One job per platform with a library client, so a slow Epic never delays Steam
(rule 4). A job does what the sync button does: every active, connected account
of its platform, each in its own run, and then the enrichment steps that follow
a sync. It runs in this process; a second process would start a second
scheduler, which is why the ADR asks for one.
"""

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Final

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from ludarium.config import ConfigurationError
from ludarium.enums import SyncStatus, SyncTrigger
from ludarium.models import Account, Provider, SyncRun
from ludarium.models.types import utcnow
from ludarium.providers.registry import supports
from ludarium.steps import Scheduled, StepContext, enrich_after_sync, plan_after_sync
from ludarium.sync import SyncInProgressError, library_for, sync_account

logger = logging.getLogger(__name__)

# The gap between platforms' first runs after a start that finds them due, so a
# restart does not ask every platform at once.
STAGGER: Final = timedelta(minutes=1)


class SyncSchedule:
    """The scheduler, its jobs, and the runs they have in flight."""

    def __init__(self, context: StepContext, scheduled: Scheduled) -> None:
        self._context = context
        self._scheduled = scheduled
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._running: set[asyncio.Task[object]] = set()

    def intervals(self, providers: Iterable[str]) -> dict[str, timedelta]:
        """Each syncable platform's interval, leaving out the ones turned off."""

        settings = self._context.settings
        unknown = sorted(set(settings.sync_intervals) - {key for key in providers if supports(key)})
        if unknown:
            raise ConfigurationError(
                f"LUDARIUM_SYNC_INTERVALS names platforms with no library client: "
                f"{', '.join(unknown)}"
            )
        hours = {
            key: settings.sync_intervals.get(key, settings.sync_interval_hours)
            for key in providers
            if supports(key)
        }
        return {key: timedelta(hours=value) for key, value in hours.items() if value > 0}

    def start(self, providers: Iterable[Provider], *, now: datetime | None = None) -> None:
        """Schedule each platform, its first run an interval after its last success.

        Counting from the last success rather than from the start is what keeps
        a restart from syncing everything again. A platform that is overdue, or
        has never synced, runs soon, one `STAGGER` after the one before it.
        """

        now = now or utcnow()
        rows = {provider.key: provider for provider in providers}
        for position, (key, interval) in enumerate(sorted(self.intervals(rows).items())):
            last = rows[key].last_success_at
            soonest = now + STAGGER * (position + 1)
            first = soonest if last is None else max(soonest, last + interval)
            self._scheduler.add_job(
                self.sync_provider,
                IntervalTrigger(seconds=interval.total_seconds(), start_date=first),
                args=[key],
                id=key,
                # A run still going when the next is due is the next run's
                # answer already; one missed while asleep is one run, not many.
                max_instances=1,
                coalesce=True,
                next_run_time=first,
            )
        self._scheduler.start()

    def next_runs(self) -> dict[str, datetime]:
        return {job.id: job.next_run_time for job in self._scheduler.get_jobs()}

    async def stop(self) -> None:
        """Cancel what is running and wait for it to close its runs as failed.

        APScheduler cancels its tasks and returns; the database would then be
        disposed under a run that was still closing itself, and the row would
        stay `running` until `ORPHAN_AFTER` reclaimed it.
        """

        # Not running when `start` refused the configuration, and the refusal
        # is what the operator needs to read, not a second error over it.
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        running = list(self._running)
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def sync_provider(self, key: str) -> list[SyncRun]:
        """Sync every active, connected account of one platform, then enrich after it."""

        task = asyncio.current_task()
        if task is not None:
            self._running.add(task)
        try:
            return await self._sync(key)
        finally:
            if task is not None:
                self._running.discard(task)

    async def _sync(self, key: str) -> list[SyncRun]:
        database = self._context.database
        async with database.session_factory() as session:
            provider = await session.scalar(select(Provider).where(Provider.key == key))
            if provider is None or not provider.enabled:
                return []
            accounts = list(
                await session.scalars(
                    select(Account.id)
                    .where(
                        Account.provider_id == provider.id,
                        Account.is_active.is_(True),
                        Account.is_derived.is_(False),
                    )
                    .order_by(Account.id)
                )
            )
            await session.commit()

        runs: list[SyncRun] = []
        for account_id in accounts:
            # A session per account, as the sync endpoint's loop has one per
            # request: one account's rollback is not another's.
            async with database.writing_session_factory() as session:
                account = await session.get_one(Account, account_id)
                library = library_for(account, key=key, client=self._context.http)
                try:
                    run = await sync_account(
                        session, account=account, library=library, trigger=SyncTrigger.SCHEDULED
                    )
                except SyncInProgressError:
                    # Someone pressed sync, or the last scheduled run is still
                    # going. Either way this account is being synced already.
                    logger.info("account %d is already syncing; skipped", account_id)
                    continue
            logger.info("scheduled `%s` sync of account %d: %s", key, account_id, run.status)
            runs.append(run)

        if any(run.status in (SyncStatus.SUCCESS, SyncStatus.PARTIAL) for run in runs):
            planned = plan_after_sync(self._context, library=key)
            self._scheduled.add(provider for provider, _ in planned)
            await enrich_after_sync(
                self._context, planned, trigger=SyncTrigger.SCHEDULED, scheduled=self._scheduled
            )
        return runs
