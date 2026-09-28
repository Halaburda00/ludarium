import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enrichment import EnrichmentRun
from ludarium.enums import SyncTrigger
from ludarium.seed import seed_providers
from ludarium.steps import Scheduled, StepContext, enrich_after_sync


def test_a_step_two_syncs_queued_stays_queued_until_both_are_done() -> None:
    scheduled = Scheduled()
    scheduled.add(["steam_store", "igdb"])
    scheduled.add(["steam_store"])

    scheduled.done("steam_store")
    assert list(scheduled) == ["steam_store", "igdb"]
    scheduled.done("steam_store")
    scheduled.done("igdb")
    assert list(scheduled) == []


async def test_a_step_that_breaks_takes_the_rest_of_the_queue_with_it(
    db: Database, session: AsyncSession
) -> None:
    """A client waiting on steps that will never run would wait for ever."""

    await seed_providers(session)

    async def broken(run: EnrichmentRun) -> None:
        raise RuntimeError("a bug in the step")

    async def never(run: EnrichmentRun) -> None:
        raise AssertionError("runs after a broken step")

    scheduled = Scheduled()
    scheduled.add(["steam_store", "igdb"])
    context = StepContext(http=None, database=db, settings=None)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        await enrich_after_sync(
            context,
            [("steam_store", broken), ("igdb", never)],
            trigger=SyncTrigger.MANUAL,
            scheduled=scheduled,
        )

    assert list(scheduled) == []


def test_the_queue_reads_in_the_order_the_steps_run() -> None:
    """Not the order they were queued in: the first one named is the one said to be running."""

    scheduled = Scheduled()
    scheduled.add(["steam_store", "igdb", "rawg"])
    scheduled.done("steam_store")
    # A second sync queues the pipeline again while the first is on IGDB.
    scheduled.add(["steam_store", "igdb", "rawg"])

    assert list(scheduled) == ["steam_store", "igdb", "rawg"]


def test_the_step_running_reads_first_whatever_order_the_steps_run_in() -> None:
    """Nothing fixes the order a sync runs its steps in to the order `STEPS` names them."""

    scheduled = Scheduled()
    scheduled.add(["igdb", "steam_store", "rawg"])
    scheduled.start("igdb")

    assert list(scheduled) == ["igdb", "steam_store", "rawg"]

    scheduled.done("igdb")
    scheduled.start("steam_store")
    assert list(scheduled) == ["steam_store", "rawg"]
