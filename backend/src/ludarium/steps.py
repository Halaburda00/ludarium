"""Which enrichment steps exist, and which library sync each one follows.

Here rather than in the API layer for the reason `providers.registry` gives: the
M4 scheduler will reach for the same table, and a step added for RAWG is a line
in each mapping rather than a branch in an endpoint.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

import httpx

from ludarium.classification import classify_steam_items
from ludarium.config import Settings
from ludarium.crypto import CredentialCipher
from ludarium.db import Database
from ludarium.enrichment import EnrichmentInProgressError, Step, enrich
from ludarium.enums import SyncTrigger
from ludarium.matching import anchor_steam_works
from ludarium.providers.igdb import IgdbClient, IgdbCredentials
from ludarium.providers.steam_store import SteamStoreClient
from ludarium.tokens import DatabaseTokenStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StepContext:
    """What a step may need to be built: the app's connection pool, database and settings."""

    http: httpx.AsyncClient
    database: Database
    settings: Settings


# None when the instance is not set up for the step — no IGDB application —
# which is a configuration the user chose, not a failure to record.
type StepBuilder = Callable[[StepContext], Step | None]


def _anchor(context: StepContext) -> Step | None:
    settings = context.settings
    if settings.igdb_client_id is None or settings.igdb_client_secret is None:
        return None
    cipher = CredentialCipher(settings.encryption_key.get_secret_value())
    client = IgdbClient(
        IgdbCredentials(settings.igdb_client_id, settings.igdb_client_secret.get_secret_value()),
        context.http,
        tokens=DatabaseTokenStore(context.database, cipher),
    )
    return anchor_steam_works(client)


STEPS: Final[Mapping[str, StepBuilder]] = {
    "steam_store": lambda context: classify_steam_items(SteamStoreClient(context.http)),
    "igdb": _anchor,
}

# A successful sync of the key is what gives each of these something new to ask
# about (ADR-0019). In order, and the order is load-bearing: matching takes only
# works known to be games, so classification has to have run first.
FOLLOWS: Final[Mapping[str, tuple[str, ...]]] = {
    "steam": ("steam_store", "igdb"),
}


async def enrich_after_sync(context: StepContext, *, library: str, trigger: SyncTrigger) -> None:
    """Run every step that follows `library`, one after another.

    A step already running is skipped rather than queued. The items this sync
    added are still asked about by the next run of that step, and before M4
    that is the next sync or a click (ADR-0020). A failed run is its own
    provider's status, which `enrich` records; nothing here has to catch it.

    A step the instance is not set up for is skipped without a run: no IGDB
    application is a choice, and recording it as a failure on every sync would
    put a red status on something nobody asked for.
    """

    for provider in FOLLOWS.get(library, ()):
        step = STEPS[provider](context)
        if step is None:
            continue
        try:
            await enrich(context.database, provider=provider, step=step, trigger=trigger)
        except EnrichmentInProgressError:
            logger.info(
                "`%s` is already enriching; this sync's items wait for its next run", provider
            )
