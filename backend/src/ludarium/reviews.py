"""Steam's user-review score, asked of the store for every work with a Steam appid (#61).

An Epic-only work has no Steam copy, but layer 1 keeps the appids IGDB gives a
matched work, and an appid is all the store needs. So a work owned only on Epic
gets the score its Steam page shows, as a Steam-owned one does.

Which reviews count is the store's default, not the endpoint's: every language,
bought on Steam, off-topic review periods left out. That is `summary_filtered`,
the one summary the store answers the same for every visitor. The row its page
calls "All Reviews" is filtered to the visitor's language, and so is a number
nobody else sees (ADR-0027).

The score is provenance like any other (rule 9): the step records what the store
says and the resolver writes the columns.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import select

from ludarium.db import Database
from ludarium.enrichment import EnrichmentRun, Step
from ludarium.enums import EntityType, SteamRating, WorkLinkRole
from ludarium.models import Account, Entitlement, EntitlementWork, ExternalId, Provider, Work
from ludarium.models.cache import Payload
from ludarium.models.types import ScalarValue
from ludarium.providers.base import whole_number
from ludarium.providers.steam_store import MAX_BATCH, SteamStoreClient
from ludarium.queries import in_batches
from ludarium.resolver import record_entities, resolve_entities

logger = logging.getLogger(__name__)

LIBRARY: Final = "steam"
RESOURCE: Final = "reviews"
SUMMARY: Final = "summary_filtered"
FIELDS: Final = (
    "steam_review_rating",
    "steam_review_percent",
    "steam_review_count",
    "steam_review_appid",
)

# The store's `review_score`, whose label it translates into whatever language
# it is asked in. Measured: 1, 4 to 9, and 0 for an app with no verdict. 2 and
# 3 were never seen; they are the two labels left, in the order the scale runs.
RATINGS: Final[Mapping[int, SteamRating]] = {
    code: rating for code, rating in enumerate(SteamRating, start=1)
}

# Scores move with every review, but a percentage over thousands hardly at all
# in a week, and the whole library costs one request per 200 apps. Shorter than
# the month an app's kind is trusted for: this is the number that does change.
MAX_AGE: Final = timedelta(days=7)


def score_steam_reviews(store: SteamStoreClient) -> Step:
    """The enrichment step, run under the `steam_store` provider."""

    async def fetch(appids: Sequence[str]) -> dict[str, Payload]:
        # Only the summary is kept, of all the store says about an app. A known
        # app without one is left out and cached as having nothing to say.
        found = await store.items(appids, reviews=True)
        summaries = {appid: _summary(item) for appid, item in found.items()}
        return {appid: summary for appid, summary in summaries.items() if summary is not None}

    async def step(run: EnrichmentRun) -> None:
        targets = await _library(run.database)
        answers = await run.fetch(
            RESOURCE,
            sorted({appid for appids in targets.values() for appid in appids}, key=int),
            fetch=fetch,
            batch_size=MAX_BATCH,
            max_age=MAX_AGE,
        )
        await _record(run, targets, answers)

    return step


def _summary(item: Mapping[str, Any]) -> Payload | None:
    reviews = item.get("reviews")
    summary = reviews.get(SUMMARY) if isinstance(reviews, dict) else None
    if not isinstance(summary, dict):
        return None
    return {
        "review_count": summary.get("review_count"),
        "percent_positive": summary.get("percent_positive"),
        "review_score": summary.get("review_score"),
    }


def chosen(appids: frozenset[str], answers: Mapping[str, Payload | None]) -> str | None:
    """The app a work's score is taken from: the one with the most reviews.

    A work can resolve to several — an edition sold as its own app, a re-release,
    the old appid IGDB still lists beside the new one. The most-reviewed is the
    page most people read, and a delisted one, with no summary, never wins. A
    tie goes to the lower appid, so the choice never depends on row order.
    """

    answered = [
        (whole_number(payload.get("review_count")) or 0, -int(appid), appid)
        for appid in appids
        if isinstance(payload := answers.get(appid), dict)
    ]
    return max(answered)[2] if answered else None


def values_of(appid: str, summary: Payload | None) -> dict[str, ScalarValue | None]:
    """The four columns for one app's summary, recorded together.

    A score is a percentage over at least one review. The verdict is optional:
    below the store's threshold — no verdict at 1, 2 and 5 reviews, one from
    16 up, in 584 real answers — the store names none and still counts the
    reviews, and a missing verdict should say how few there are rather than
    erase them (ADR-0029). The count is what tells 100% of one review from 96%
    of a million, and it is always kept beside the percentage.

    None is an answer, not a gap: an app with no reviews has no score, and a
    column that kept an older one would show a score no source stands behind.
    """

    none: dict[str, ScalarValue | None] = dict.fromkeys(FIELDS)
    if not isinstance(summary, dict):
        return none
    code = whole_number(summary.get("review_score"))
    # 0 is the store's "no verdict"; a code it has never used is treated alike.
    rating = RATINGS.get(code) if code is not None else None
    percent = whole_number(summary.get("percent_positive"))
    count = whole_number(summary.get("review_count"))
    if percent is None or not 0 <= percent <= 100 or not count:
        return none
    return {
        "steam_review_rating": rating.value if rating is not None else None,
        "steam_review_percent": percent,
        "steam_review_count": count,
        "steam_review_appid": appid,
    }


async def _library(database: Database) -> dict[int, frozenset[str]]:
    """Every work with a Steam appid, and all of its appids.

    Those it is owned under, matched or not — a Steam copy's own page is not a
    guess. Removed entitlements included, as in classification: a game that left
    the account is still that game. And those IGDB gives a matched work, which
    layer 1 keeps: the only appid an Epic-only game has.
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
            .join(Account, Account.id == Entitlement.account_id)
            .join(Provider, Provider.id == Account.provider_id)
            .where(
                Provider.key == LIBRARY,
                Entitlement.provider_item_id.is_not(None),
                # Reported ids, not appids Steam handed over (ADR-0039).
                Account.is_derived.is_(False),
            )
        ):
            appids.setdefault(work_id, set()).add(str(appid))
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
        # A non-numeric id is nothing the store could be asked about, and the
        # client refuses a batch holding one.
        return {
            work_id: frozenset(appid for appid in owned if appid.isdigit())
            for work_id, owned in sorted(appids.items())
            if any(appid.isdigit() for appid in owned)
        }


async def _record(
    run: EnrichmentRun,
    targets: Mapping[int, frozenset[str]],
    answers: Mapping[str, Payload | None],
) -> None:
    """Each work's score from its chosen app, a transaction per batch of works.

    A work none of whose apps the store answered for is left as it was: the
    store forgetting a delisted app is not evidence its score changed. Batches
    are committed as they are written, so a sync waits for one batch rather
    than the library (#114).
    """

    scores: dict[int, dict[str, ScalarValue | None]] = {}
    for work_id, appids in targets.items():
        appid = chosen(appids, answers)
        if appid is not None:
            scores[work_id] = values_of(appid, answers[appid])
    scored = sum(values["steam_review_percent"] is not None for values in scores.values())
    judged = sum(values["steam_review_rating"] is not None for values in scores.values())
    for batch in in_batches(list(scores)):
        async with run.database.writing_session_factory() as session:
            reporter = await session.get_one(Provider, run.provider_id)
            recorded = await record_entities(
                session,
                entity_type=EntityType.WORK,
                source_kind=reporter.source_kind,
                source_ref=reporter.key,
                values={work_id: scores[work_id] for work_id in batch},
                run_id=run.id,
            )
            await resolve_entities(session, entity_type=EntityType.WORK, recorded=recorded)
            await session.commit()
    logger.info(
        "%d of %d works with a Steam appid have a Steam review score, %d with a verdict",
        scored,
        len(targets),
        judged,
    )
