"""Which enrichment steps exist, and which library sync each one follows.

Here rather than in the API layer for the reason `providers.registry` gives: the
M4 scheduler will reach for the same table, and a step added for RAWG is a line
in each mapping rather than a branch in an endpoint.
"""

import logging
from collections.abc import Callable, Mapping
from typing import Final

import httpx

from ludarium.classification import classify_steam_items
from ludarium.db import Database
from ludarium.enrichment import EnrichmentInProgressError, Step, enrich
from ludarium.enums import SyncTrigger
from ludarium.providers.steam_store import SteamStoreClient

logger = logging.getLogger(__name__)

type StepBuilder = Callable[[httpx.AsyncClient], Step]

STEPS: Final[Mapping[str, StepBuilder]] = {
    "steam_store": lambda http: classify_steam_items(SteamStoreClient(http)),
}

# A successful sync of the key is what gives each of these something new to ask
# about (ADR-0019).
FOLLOWS: Final[Mapping[str, tuple[str, ...]]] = {
    "steam": ("steam_store",),
}


async def enrich_after_sync(
    database: Database, http: httpx.AsyncClient, *, library: str, trigger: SyncTrigger
) -> None:
    """Run every step that follows `library`, one after another.

    A step already running is skipped rather than queued. The items this sync
    added are still asked about by the next run of that step, and before M4
    that is the next sync or a click (ADR-0020). A failed run is its own
    provider's status, which `enrich` records; nothing here has to catch it.
    """

    for provider in FOLLOWS.get(library, ()):
        try:
            await enrich(database, provider=provider, step=STEPS[provider](http), trigger=trigger)
        except EnrichmentInProgressError:
            logger.info(
                "`%s` is already enriching; this sync's items wait for its next run", provider
            )
