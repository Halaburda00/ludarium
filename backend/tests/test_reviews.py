import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_provider, make_work
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import queries
from ludarium import reviews as reviews_module
from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import EntityType, SourceKind, SteamRating, SyncStatus
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    ExternalId,
    FetchCache,
    FieldProvenance,
    Work,
)
from ludarium.providers import steam_store as store_module
from ludarium.providers.steam_store import SteamStoreClient
from ludarium.resolver import record, resolve
from ludarium.reviews import RATINGS, chosen, score_steam_reviews, values_of

FIXTURES = Path(__file__).parent / "fixtures" / "steam_store"
GET_ITEMS_URL = f"{store_module.STORE_API}{store_module.GET_ITEMS}"


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def asked_for(request: httpx.Request) -> list[int]:
    return [entry["appid"] for entry in json.loads(request.url.params["input_json"])["ids"]]


def the_store(request: httpx.Request) -> httpx.Response:
    """The recorded answer, cut down to the apps this request named, as the store would."""

    ids = asked_for(request)
    # `few_reviews.json` is one app the store names no verdict for, its summary
    # as a real instance cached it (ADR-0029); the rest is one recorded answer.
    items = [
        item
        for name in ("reviews.json", "few_reviews.json")
        for item in recorded(name)["response"]["store_items"]
        if item["id"] in ids
    ]
    return httpx.Response(200, json={"response": {"store_items": items}})


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_module, "RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(store_module, "RETRY_MAX_WAIT_SECONDS", 0.0)


@pytest.fixture
async def store() -> AsyncIterator[SteamStoreClient]:
    async with httpx.AsyncClient() as client:
        yield SteamStoreClient(client)


@pytest.fixture
async def steam(session: AsyncSession) -> Account:
    account = await make_account(session)
    await make_provider(session, key="steam_store")
    await session.commit()
    return account


async def own(
    session: AsyncSession, account: Account, appid: str, work: Work | None = None
) -> Work:
    """An entitlement and the stub a sync would have made for it, or a link to `work`."""

    work = work or await make_work(session, f"App {appid}")
    entitlement = Entitlement(
        account_id=account.id, provider_item_id=appid, provider_title="An app"
    )
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    await session.flush()
    return work


async def given_by_igdb(session: AsyncSession, work: Work, appid: str) -> None:
    """The Steam appid layer 1 keeps for a matched work, as it does for an Epic-only one."""

    work.is_matched = True
    session.add(
        ExternalId(
            entity_type=EntityType.WORK,
            entity_id=work.id,
            namespace="steam",
            value=appid,
            is_authoritative=True,
            source_ref="igdb",
        )
    )
    await session.flush()


async def scores(db: Database) -> dict[int, tuple[Any, ...]]:
    async with db.session_factory() as reader:
        rows = await reader.execute(
            select(
                Work.id,
                Work.steam_review_rating,
                Work.steam_review_percent,
                Work.steam_review_count,
                Work.steam_review_appid,
            )
        )
        return {work_id: tuple(rest) for work_id, *rest in rows}


@respx.mock
async def test_a_failed_batch_keeps_the_batches_before_it(
    db: Database,
    session: AsyncSession,
    steam: Account,
    store: SteamStoreClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each batch commits on its own, so a sync waits for one batch, not the library (#114)."""

    monkeypatch.setattr(queries, "BIND_LIMIT", 2)
    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    works = [await own(session, steam, appid) for appid in ("292030", "620", "201510", "378649")]
    await session.commit()
    resolve_entities = reviews_module.resolve_entities

    async def failing_second(*args: Any, **kwargs: Any) -> Any:
        if any(works[2].id in batch for batch in kwargs.values() if isinstance(batch, dict)):
            raise RuntimeError("the second batch fails")
        return await resolve_entities(*args, **kwargs)

    monkeypatch.setattr(reviews_module, "resolve_entities", failing_second)

    with pytest.raises(RuntimeError):
        await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    stored = await scores(db)
    assert [stored[work.id][1] is not None for work in works] == [True, True, False, False]


@respx.mock
async def test_a_steam_copy_takes_the_score_its_store_page_shows(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "620")
    await session.commit()

    run = await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert run.status is SyncStatus.SUCCESS
    # Portal 2, all languages, bought on Steam: the store's default filter.
    assert (await scores(db))[work.id] == (SteamRating.OVERWHELMINGLY_POSITIVE, 98, 390695, "620")


@respx.mock
async def test_the_score_counts_every_language_not_only_english(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """The Witcher 3 in English alone is 242 156 reviews; the page a visitor in another
    language reads counts theirs. Only `summary_filtered` is the same for all of them."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "292030")
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert (await scores(db))[work.id][2] == 824691


@respx.mock
async def test_an_epic_only_game_is_scored_by_the_appid_igdb_gives_it(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await make_work(session, "Portal 2")
    await given_by_igdb(session, work, "620")
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert (await scores(db))[work.id][3] == "620"


@respx.mock
async def test_an_unmatched_work_s_stray_appid_is_not_asked_about(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Only a match makes IGDB's appid the work's; an undone one leaves it no one's."""

    route = respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await make_work(session, "Portal 2")
    await given_by_igdb(session, work, "620")
    work.is_matched = False
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert not route.called
    assert (await scores(db))[work.id] == (None, None, None, None)


@respx.mock
async def test_of_several_apps_the_most_reviewed_one_is_chosen(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """The Witcher 3's old GOTY appid, delisted, and a DLC with fewer reviews both lose."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "378649")
    await own(session, steam, "499450", work)
    await given_by_igdb(session, work, "292030")
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert (await scores(db))[work.id][3] == "292030"


def test_a_tie_goes_to_the_lower_appid_whatever_the_order() -> None:
    summary = {"review_count": 10, "percent_positive": 90, "review_score": 7}
    answers = {"20": summary, "3": summary}

    assert chosen(frozenset({"20", "3"}), answers) == "3"
    assert chosen(frozenset({"3", "20"}), answers) == "3"


@respx.mock
async def test_an_app_with_no_verdict_has_no_score(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """A playtest: `review_score` 0 and `percent_positive` 0, which is not 0% positive."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "1611740")
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert (await scores(db))[work.id] == (None, None, None, None)
    rows = (await session.scalars(select(FieldProvenance))).all()
    # Recorded as none rather than skipped: it is the store's answer.
    assert {row.field for row in rows} == {
        "steam_review_rating",
        "steam_review_percent",
        "steam_review_count",
        "steam_review_appid",
    }
    assert {row.value for row in rows} == {None}


@respx.mock
async def test_a_score_the_store_stops_answering_for_is_kept(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    route = respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "620")
    await session.commit()
    await enrich(db, provider="steam_store", step=score_steam_reviews(store))
    await session.execute(FetchCache.__table__.delete())
    await session.commit()
    delisted = {"id": 620, "success": 15, "appid": 0}
    route.mock(
        side_effect=lambda _: httpx.Response(200, json={"response": {"store_items": [delisted]}})
    )

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert (await scores(db))[work.id][0] is SteamRating.OVERWHELMINGLY_POSITIVE


@respx.mock
async def test_the_score_is_a_provenance_row_the_resolver_chose(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Rule 9: the step records, and the columns are the resolver's."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "201510")
    await session.commit()

    run = await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    rows = {
        row.field: row
        for row in await session.scalars(
            select(FieldProvenance).where(FieldProvenance.entity_id == work.id)
        )
    }
    rating = rows["steam_review_rating"]
    assert (rating.source_kind, rating.source_ref) == (SourceKind.PLATFORM_API, "steam_store")
    # FlatOut 3, the one code-1 app measured.
    assert rating.value == "overwhelmingly_negative"
    assert rating.run_id == run.id
    assert all(row.is_effective for row in rows.values())


@respx.mock
async def test_a_user_s_own_score_outlives_the_store(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "620")
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field="steam_review_percent",
        source_kind=SourceKind.MANUAL,
        source_ref="manual",
        value=50,
    )
    await resolve(
        session, entity_type=EntityType.WORK, entity_id=work.id, fields=["steam_review_percent"]
    )
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert (await scores(db))[work.id][1] == 50


@respx.mock
async def test_a_second_run_inside_a_week_asks_the_store_nothing(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    route = respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    await own(session, steam, "620")
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))
    run = await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert route.call_count == 1
    assert (run.items_seen, run.items_updated) == (1, 0)


@respx.mock
async def test_a_score_the_old_code_threw_away_is_filled_from_the_cache(
    db: Database,
    session: AsyncSession,
    steam: Account,
    store: SteamStoreClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache holds the store's summary, not the columns, so the next run redoes them.

    Before #107 a summary with no verdict was recorded as four nulls. The first
    run after the upgrade, inside the week the answer is cached for, asks the
    store nothing and still records the percentage and the count.
    """

    route = respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "2914390")
    await session.commit()
    before = reviews_module.values_of

    def without_verdicts(appid: str, summary: Any) -> dict[str, Any]:
        values = before(appid, summary)
        return values if values["steam_review_rating"] else dict.fromkeys(values)

    monkeypatch.setattr(reviews_module, "values_of", without_verdicts)
    await enrich(db, provider="steam_store", step=score_steam_reviews(store))
    assert (await scores(db))[work.id] == (None, None, None, None)

    monkeypatch.setattr(reviews_module, "values_of", before)
    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert route.call_count == 1
    assert (await scores(db))[work.id] == (None, 80, 5, "2914390")


@respx.mock
async def test_only_the_summary_is_cached(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Not the prices and categories the store sends beside it."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    await own(session, steam, "292030")
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    cached = (await session.scalars(select(FetchCache))).one()
    assert (cached.resource, cached.key) == ("reviews", "292030")
    assert cached.payload == {"review_count": 824691, "percent_positive": 96, "review_score": 9}


@pytest.mark.parametrize(
    "summary",
    [
        {"review_count": 26, "percent_positive": 101, "review_score": 7},
        {"review_count": 0, "percent_positive": 92, "review_score": 7},
        {"review_count": 26, "percent_positive": True, "review_score": 7},
        {"review_count": 26, "percent_positive": None, "review_score": 7},
        {"review_count": None, "percent_positive": 92, "review_score": 7},
    ],
)
def test_a_summary_that_is_not_a_score_is_recorded_as_none(summary: dict[str, Any]) -> None:
    assert set(values_of("931180", summary).values()) == {None}


@pytest.mark.parametrize(
    "summary",
    [
        # The store's "no verdict", as it answers for an app with five reviews.
        {"review_count": 5, "percent_positive": 80, "review_score": 0},
        # A code it has never used, and one that is not a number: no verdict
        # this code knows, over reviews that are still counted.
        {"review_count": 5, "percent_positive": 80, "review_score": 10},
        {"review_count": 5, "percent_positive": 80, "review_score": "7"},
    ],
)
def test_a_score_without_a_verdict_keeps_its_percentage_and_count(summary: dict[str, Any]) -> None:
    assert values_of("2914390", summary) == {
        "steam_review_rating": None,
        "steam_review_percent": 80,
        "steam_review_count": 5,
        "steam_review_appid": "2914390",
    }


@respx.mock
async def test_a_game_with_too_few_reviews_for_a_verdict_is_scored_on_them(
    db: Database, session: AsyncSession, steam: Account, store: SteamStoreClient
) -> None:
    """Five reviews, no verdict: 80% of five is kept, with the five."""

    respx.get(GET_ITEMS_URL).mock(side_effect=the_store)
    work = await own(session, steam, "2914390")
    await session.commit()

    await enrich(db, provider="steam_store", step=score_steam_reviews(store))

    assert (await scores(db))[work.id] == (None, 80, 5, "2914390")


def test_the_scale_runs_from_the_worst_verdict_to_the_best() -> None:
    assert RATINGS[1] is SteamRating.OVERWHELMINGLY_NEGATIVE
    assert RATINGS[5] is SteamRating.MIXED
    assert RATINGS[9] is SteamRating.OVERWHELMINGLY_POSITIVE
    assert 0 not in RATINGS
