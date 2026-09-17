"""What kind of thing each Steam app in the library is, asked of Steam's store (#41).

It runs ahead of the matcher on purpose. Layer 1 resolves an appid to a game,
and a playtest's appid resolves to the game it tests: a false positive that rule
6 ranks below leaving it unmatched. With `work.item_kind` null until something
classifies it, the matcher can take only what is known to be a game.

The store is a provider like any other here: the step records what the store
asserts and the resolver decides (rule 9), so a user's own label still wins
(rule 3).
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Final

from sqlalchemy import select

from ludarium.db import Database
from ludarium.enrichment import EnrichmentRun, Step
from ludarium.enums import EntityType, ItemKind, WorkLinkRole
from ludarium.models import Account, Entitlement, EntitlementWork, Provider
from ludarium.models.cache import Payload
from ludarium.providers.base import whole_number
from ludarium.providers.steam_store import MAX_BATCH, SteamStoreClient
from ludarium.resolver import record_many, resolve

# Whose entitlements are classified: the store answers about Steam appids only.
LIBRARY: Final = "steam"
RESOURCE: Final = "items"

# Steam's numbering, measured rather than documented (ADR-0020). A code missing
# here asserts nothing, rather than a guess: `video` is an `ItemKind`, and no
# sample has shown which number the store gives one.
STORE_TYPES: Final[Mapping[int, ItemKind]] = {
    0: ItemKind.GAME,
    1: ItemKind.DEMO,
    2: ItemKind.MOD,
    4: ItemKind.DLC,
    6: ItemKind.TOOL,
    11: ItemKind.SOUNDTRACK,
    12: ItemKind.PLAYTEST,
}

# An app's type rarely changes, but the store does reclassify, and asking again
# costs one request per 200 apps. A month keeps a library honest for the price
# of a few requests.
MAX_AGE: Final = timedelta(days=30)


def kind_of(item: Payload | None) -> ItemKind | None:
    """The kind the store gives an app, or None where it gives none this code knows."""

    if not isinstance(item, dict):
        return None
    code = whole_number(item.get("type"))
    return None if code is None else STORE_TYPES.get(code)


def classify_steam_items(store: SteamStoreClient) -> Step:
    """The enrichment step, run under the `steam_store` provider."""

    async def step(run: EnrichmentRun) -> None:
        targets = await _library(run.database)
        answers = await run.fetch(
            RESOURCE,
            [appid for appid, _ in targets],
            fetch=store.items,
            batch_size=MAX_BATCH,
            max_age=MAX_AGE,
        )
        await _record(run, targets, answers)

    return step


async def _library(database: Database) -> list[tuple[str, int]]:
    """Every Steam appid the library holds, with the work it is the primary route to.

    Removed entitlements included: what an app is does not change when it
    leaves the account, and one restored keeps its kind. A manual entitlement
    carries no appid and is never here (rule 2).
    """

    async with database.reading_session_factory() as session:
        rows = await session.execute(
            select(Entitlement.provider_item_id, EntitlementWork.work_id)
            .join(
                EntitlementWork,
                (EntitlementWork.entitlement_id == Entitlement.id)
                & (EntitlementWork.role == WorkLinkRole.PRIMARY),
            )
            .join(Account, Account.id == Entitlement.account_id)
            .join(Provider, Provider.id == Account.provider_id)
            .where(Provider.key == LIBRARY, Entitlement.provider_item_id.is_not(None))
            .order_by(Entitlement.id)
        )
        # Not null by the predicate above; mypy cannot see that.
        return [(str(appid), work_id) for appid, work_id in rows]


async def _record(
    run: EnrichmentRun,
    targets: Sequence[tuple[str, int]],
    answers: Mapping[str, Payload | None],
) -> None:
    """Assert each known kind on its work and resolve it, in one transaction.

    An app the store does not know, or gives a type nothing maps, is skipped
    rather than recorded as null. Under `precedence` a null row still ranks, and
    Steam saying nothing would then outrank IGDB saying something. A kind the
    store asserted before and no longer answers for keeps its row: the store
    forgetting a delisted app is not evidence that the app changed.

    No provider is asked inside this transaction; `fetch` has already finished.
    """

    async with run.database.writing_session_factory() as session:
        reporter = await session.get_one(Provider, run.provider_id)
        for appid, work_id in targets:
            kind = kind_of(answers[appid])
            if kind is None:
                continue
            recorded = await record_many(
                session,
                entity_type=EntityType.WORK,
                entity_id=work_id,
                source_kind=reporter.source_kind,
                source_ref=reporter.key,
                values={"item_kind": kind.value},
                run_id=run.id,
            )
            await resolve(
                session,
                entity_type=EntityType.WORK,
                entity_id=work_id,
                fields=["item_kind"],
                recorded=recorded,
            )
        await session.commit()
