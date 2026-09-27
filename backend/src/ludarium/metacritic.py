"""Metacritic scores, asked of RAWG for every work IGDB has matched (#49).

RAWG has no lookup by a store's id, only a search by name, and a name is not an
identity: *Prey* (2006) and *Prey* (2017) share one. So a search only proposes,
and a candidate is believed when RAWG lists it under a Steam link naming an
appid the work is owned under — a hard id, as layer 1's is. A work none of whose
candidates carries one gets no score: a missing score costs less than someone
else's (rule 6).

Only matched works are asked about, by the name IGDB gave them. That name is
canonical English, where a stub's is whatever the store called it and a user's
own may be in any language.

The score and its link are provenance like any other (rule 9), and RAWG's slug
is an `external_id`, from which the listing builds the link to the game's page
that RAWG's terms require wherever a score is shown.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enrichment import EnrichmentRun, Step
from ludarium.enums import EntityType, WorkLinkRole
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    ExternalId,
    FieldProvenance,
    Provider,
    Work,
)
from ludarium.models.cache import Payload
from ludarium.providers.base import MalformedResponseError, whole_number
from ludarium.providers.rawg import RawgClient, steam_appids
from ludarium.resolver import record_many, resolve

logger = logging.getLogger(__name__)

LIBRARY: Final = "steam"
NAMESPACE: Final = "rawg"
# Whose name a work is searched under, where it has one: IGDB's.
NAMER: Final = "igdb"
SEARCH: Final = "games/search"
STORES: Final = "games/stores"
GAMES: Final = "games"

# Scores move while a game is new and hardly at all after. A month matches the
# other steps, and a library of a thousand matched games costs about three
# thousand of the 20 000 requests the free tier allows in one.
MAX_AGE: Final = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Target:
    work_id: int
    title: str
    appids: frozenset[str]


def score_matched_works(rawg: RawgClient) -> Step:
    """The enrichment step, run under the `rawg` provider."""

    async def search(titles: Sequence[str]) -> dict[str, Payload]:
        # Only the ids are kept. Each search is a question about one title, and
        # whatever else RAWG says about its candidates is not ours to store.
        (title,) = titles
        candidates = [_id(row, "a search result") for row in await rawg.search(title)]
        return {title: {"candidates": candidates}}

    async def stores(ids: Sequence[str]) -> dict[str, Payload]:
        (game,) = ids
        return {game: {"steam": sorted(steam_appids(await rawg.stores(int(game))))}}

    async def games(ids: Sequence[str]) -> dict[str, Payload]:
        (game,) = ids
        record = await rawg.game(int(game))
        if record is None:
            return {}
        return {
            game: {
                "slug": record.get("slug"),
                "metacritic": record.get("metacritic"),
                "metacritic_url": record.get("metacritic_url"),
            }
        }

    async def step(run: EnrichmentRun) -> None:
        targets = await _matched(run.database)
        found = await run.fetch(
            SEARCH,
            sorted({target.title for target in targets}),
            fetch=search,
            batch_size=1,
            max_age=MAX_AGE,
        )
        confirmed: dict[int, int] = {}
        for target in targets:
            for candidate in _candidates(found.get(target.title)):
                sold = await run.fetch(
                    STORES, [str(candidate)], fetch=stores, batch_size=1, max_age=MAX_AGE
                )
                if target.appids & set(_steam(sold[str(candidate)])):
                    confirmed[target.work_id] = candidate
                    break
        records = await run.fetch(
            GAMES,
            sorted({str(game) for game in confirmed.values()}, key=int),
            fetch=games,
            batch_size=1,
            max_age=MAX_AGE,
        )
        await _record(run, confirmed, records)
        logger.info(
            "%d of %d matched works confirmed in RAWG by their Steam appid",
            len(confirmed),
            len(targets),
        )

    return step


def _id(row: Mapping[str, object], what: str) -> int:
    game = whole_number(row.get("id"))
    if game is None:
        raise MalformedResponseError(f"rawg returned {what} without an id")
    return game


def _candidates(payload: Payload | None) -> list[int]:
    if not isinstance(payload, dict):
        return []
    return [game for game in payload.get("candidates", []) if whole_number(game) is not None]


def _steam(payload: Payload | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return [appid for appid in payload.get("steam", []) if isinstance(appid, str)]


async def _matched(database: Database) -> list[Target]:
    """Every matched work with the Steam appids it is owned under, oldest first.

    Removed entitlements included, as in classification and layer 1: a game
    that left the account is still that game, and a restored one keeps its score.
    """

    async with database.reading_session_factory() as session:
        appids: dict[int, set[str]] = {}
        for appid, work_id in await session.execute(
            select(Entitlement.provider_item_id, EntitlementWork.work_id)
            .join(
                EntitlementWork,
                (EntitlementWork.entitlement_id == Entitlement.id)
                & (EntitlementWork.role == WorkLinkRole.PRIMARY),
            )
            .join(Work, Work.id == EntitlementWork.work_id)
            .join(Account, Account.id == Entitlement.account_id)
            .join(Provider, Provider.id == Account.provider_id)
            .where(
                Provider.key == LIBRARY,
                Entitlement.provider_item_id.is_not(None),
                Work.is_matched.is_(True),
            )
        ):
            appids.setdefault(work_id, set()).add(str(appid))
        # And the appids IGDB gives a game owned elsewhere, which layer 1 keeps
        # for exactly this: an Epic-only game has no Steam copy to read one from.
        for work_id, appid in await session.execute(
            select(ExternalId.entity_id, ExternalId.value)
            .join(Work, Work.id == ExternalId.entity_id)
            .where(
                ExternalId.entity_type == EntityType.WORK,
                ExternalId.namespace == LIBRARY,
                Work.is_matched.is_(True),
            )
        ):
            appids.setdefault(work_id, set()).add(appid)
        titles = await _titles(session, sorted(appids))
        return [
            Target(work_id, titles[work_id], frozenset(owned))
            for work_id, owned in sorted(appids.items())
        ]


async def _titles(session: AsyncSession, work_ids: Sequence[int]) -> dict[int, str]:
    """IGDB's name for each work, or the resolved title where IGDB gave none."""

    titles = {
        work_id: title
        for work_id, title in await session.execute(
            select(Work.id, Work.title).where(Work.id.in_(work_ids))
        )
    }
    for work_id, value in await session.execute(
        select(FieldProvenance.entity_id, FieldProvenance.value).where(
            FieldProvenance.entity_type == EntityType.WORK,
            FieldProvenance.entity_id.in_(work_ids),
            FieldProvenance.field == "title",
            FieldProvenance.source_ref == NAMER,
        )
    ):
        if isinstance(value, str) and value.strip():
            titles[work_id] = value
    return titles


async def _record(
    run: EnrichmentRun, confirmed: Mapping[int, int], records: Mapping[str, Payload | None]
) -> None:
    """The score, its Metacritic link and RAWG's slug for every confirmed work, in one transaction.

    A game RAWG gives no score is recorded as none, which is an answer: the
    column goes null rather than keeping one no source stands behind. A work
    RAWG did not confirm this time is left as it was — failing to find a game
    again is not evidence its score changed.
    """

    async with run.database.writing_session_factory() as session:
        reporter = await session.get_one(Provider, run.provider_id)
        for work_id, game in confirmed.items():
            record = records.get(str(game))
            if not isinstance(record, dict):
                continue
            await _identify(session, reporter.key, work_id, record.get("slug"))
            score = whole_number(record.get("metacritic"))
            url = record.get("metacritic_url")
            values = {
                # Metacritic's scale; anything else is not a score to show.
                "metacritic_score": score if score is not None and 0 <= score <= 100 else None,
                "metacritic_url": url
                if isinstance(url, str) and url.startswith("https://")
                else None,
            }
            recorded = await record_many(
                session,
                entity_type=EntityType.WORK,
                entity_id=work_id,
                source_kind=reporter.source_kind,
                source_ref=reporter.key,
                values=values,
                run_id=run.id,
            )
            await resolve(
                session,
                entity_type=EntityType.WORK,
                entity_id=work_id,
                fields=list(values),
                recorded=recorded,
            )
        await session.commit()


async def _identify(session: AsyncSession, source: str, work_id: int, slug: object) -> None:
    """RAWG's slug for the work, the one the attribution link is built from.

    Not authoritative: the matcher inferred it, even if from a hard id. A slug
    another work already holds is left with it and logged — two works confirmed
    to one RAWG game is a merge the matcher missed, not a second owner.
    """

    if not isinstance(slug, str) or not slug.strip():
        return
    held = await session.scalar(
        select(ExternalId).where(
            ExternalId.namespace == NAMESPACE,
            ExternalId.entity_type == EntityType.WORK,
            ExternalId.value == slug,
        )
    )
    if held is not None:
        if held.entity_id != work_id:
            logger.warning(
                "rawg game %s is already work %d's, not %d's", slug, held.entity_id, work_id
            )
        return
    own = await session.scalar(
        select(ExternalId).where(
            ExternalId.namespace == NAMESPACE,
            ExternalId.entity_type == EntityType.WORK,
            ExternalId.entity_id == work_id,
        )
    )
    if own is None:
        session.add(
            ExternalId(
                entity_type=EntityType.WORK,
                entity_id=work_id,
                namespace=NAMESPACE,
                value=slug,
                source_ref=source,
            )
        )
    else:
        own.value = slug
    await session.flush()
