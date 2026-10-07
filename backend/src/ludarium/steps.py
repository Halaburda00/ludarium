"""Which enrichment steps exist, and which library sync each one follows.

Here rather than in the API layer for the reason `providers.registry` gives: the
M4 scheduler will reach for the same table, and a step added for RAWG is a line
in each mapping rather than a branch in an endpoint.
"""

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import httpx

from ludarium.classification import classify_steam_items
from ludarium.config import Settings
from ludarium.covers import fetch_covers
from ludarium.crypto import CredentialCipher
from ludarium.db import Database
from ludarium.details import describe_matched_works
from ludarium.enrichment import EnrichmentInProgressError, Step, chained, enrich
from ludarium.enums import SyncTrigger
from ludarium.matching import anchor_epic_works, anchor_gog_works, anchor_steam_works
from ludarium.metacritic import score_matched_works
from ludarium.providers.epic import EpicCatalog
from ludarium.providers.igdb import IgdbClient, IgdbCredentials
from ludarium.providers.rawg import RawgClient
from ludarium.providers.steam_store import SteamStoreClient
from ludarium.reviews import score_steam_reviews
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
    # One run: details and covers are fetched for what anchoring has just matched.
    return chained(
        anchor_steam_works(client),
        # After Steam, so an Epic copy of a game already anchored through its
        # appid is folded into that work rather than anchored beside it.
        anchor_epic_works(client, EpicCatalog(context.http)),
        # After both for the same reason: a GOG copy of a game already anchored
        # elsewhere folds into that work.
        anchor_gog_works(client),
        describe_matched_works(client),
        fetch_covers(client, settings.data_dir),
    )


def _score(context: StepContext) -> Step | None:
    key = context.settings.rawg_api_key
    if key is None:
        return None
    return score_matched_works(RawgClient(key.get_secret_value(), context.http))


def _store(context: StepContext) -> Step:
    store = SteamStoreClient(context.http)
    # One run of one endpoint, so one status: a store outage fails both.
    return chained(classify_steam_items(store), score_steam_reviews(store))


STEPS: Final[Mapping[str, StepBuilder]] = {
    "steam_store": _store,
    "igdb": _anchor,
    "rawg": _score,
}

# A successful sync of the key is what gives each of these something new to ask
# about (ADR-0019). In order, and the order is load-bearing: matching takes only
# works known to be games, so classification has to have run first, and RAWG is
# asked only about works matching has anchored.
FOLLOWS: Final[Mapping[str, tuple[str, ...]]] = {
    "steam": ("steam_store", "igdb", "rawg"),
    # Epic states each item's kind in the library itself (#64), so the store
    # comes after matching rather than before: its review scores are for the
    # Steam appids IGDB has just given the Epic games.
    "epic": ("igdb", "steam_store", "rawg"),
    # GOG states each item's kind in its library too, so it follows Epic's order.
    "gog": ("igdb", "steam_store", "rawg"),
}


class Scheduled:
    """The steps syncs have queued and not yet finished, for a client to wait on (#70).

    Filled when a sync answers and emptied as each step ends, so there is no
    moment between the answer and the first step, or between two steps, when
    nothing reads as in progress: a run row exists only while a step is
    running. In memory, because it describes this process's background tasks,
    which do not outlive it either.

    Counted rather than a set: a second sync can queue a step the first one's
    has not finished, and the first finishing must not clear the second's.
    """

    def __init__(self) -> None:
        self._queued: Counter[str] = Counter()
        self._running: Counter[str] = Counter()

    def add(self, providers: Iterable[str]) -> None:
        self._queued.update(providers)

    def start(self, provider: str) -> None:
        self._running[provider] += 1

    def done(self, provider: str) -> None:
        for counts in (self._queued, self._running):
            counts[provider] -= 1
            if counts[provider] <= 0:
                del counts[provider]

    def __iter__(self) -> Iterator[str]:
        # What is running first, then what waits, each in the order `STEPS`
        # names them rather than the order they were queued: a step queued
        # again by a second sync would otherwise read as the last one. Running
        # first because the order is not fixed — an Epic sync asks the store
        # after IGDB — and a client names the first as the one running.
        order = {provider: position for position, provider in enumerate(STEPS)}
        return iter(
            sorted(
                self._queued,
                key=lambda provider: (
                    provider not in self._running,
                    order.get(provider, len(order)),
                ),
            )
        )


def plan_after_sync(context: StepContext, *, library: str) -> list[tuple[str, Step]]:
    """Every step that follows `library` and that this instance is set up for, in order.

    Built before the sync answers, so the answer can name them. A step the
    instance is not set up for — no IGDB application, no RAWG key — is left
    out rather than planned: it opens no run, and a client waiting on it would
    wait for ever. Recording that as a failure on every sync would put a red
    status on something nobody asked for.
    """

    planned = []
    for provider in FOLLOWS.get(library, ()):
        step = STEPS[provider](context)
        if step is not None:
            planned.append((provider, step))
    return planned


async def enrich_after_sync(
    context: StepContext,
    planned: Sequence[tuple[str, Step]],
    *,
    trigger: SyncTrigger,
    scheduled: Scheduled,
) -> None:
    """Run the planned steps one after another, and tell `scheduled` as each one ends.

    A step already running is skipped rather than queued. The items this sync
    added are still asked about by the next run of that step, and before M4
    that is the next sync or a click (ADR-0020). A failed run is its own
    provider's status, which `enrich` records; nothing here has to catch it.
    """

    remaining = [provider for provider, _ in planned]
    try:
        for provider, step in planned:
            scheduled.start(provider)
            try:
                await enrich(context.database, provider=provider, step=step, trigger=trigger)
            except EnrichmentInProgressError:
                logger.info(
                    "`%s` is already enriching; this sync's items wait for its next run", provider
                )
            finally:
                scheduled.done(provider)
                remaining.remove(provider)
    finally:
        # Whatever ended the task early — a bug in a step, a cancelled task at
        # shutdown — the steps it will never run are not in progress either.
        for provider in remaining:
            scheduled.done(provider)
