import json
from base64 import urlsafe_b64encode
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import steps as steps_module
from ludarium.api import works as works_module
from ludarium.enums import (
    CompanyRole,
    EntitlementOrigin,
    EntityType,
    ImageKind,
    SteamRating,
    WorkLinkRole,
)
from ludarium.models import (
    Account,
    AppUser,
    Company,
    Entitlement,
    EntitlementWork,
    ExternalId,
    ImageAsset,
    Provider,
    UserWorkState,
    Work,
    WorkCompany,
)
from ludarium.models.types import utcnow
from ludarium.providers import steam as steam_module

FIXTURES = Path(__file__).parent / "fixtures" / "steam"
OWNED_GAMES_URL = f"{steam_module.STEAM_API}{steam_module.OWNED_GAMES}"
API_KEY = "0123456789ABCDEF-not-a-real-key"
STEAM_ID = "76561197960287930"

# The recorded fixture, in `sort_title` order — the leading article moved.
LIBRARY = ["Dota 2", "Portal 2", "The Witcher 3: Wild Hunt"]


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def no_enrichment_after_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a sync triggers is `test_enrichment_api`'s subject, and it asks the store."""

    monkeypatch.setattr(steps_module, "FOLLOWS", {})


@pytest.fixture
def synced(client: TestClient) -> TestClient:
    """Signed in, connected and synced once: the state M1 is finished in."""

    assert (
        client.post(
            "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
        ).status_code
        == 200
    )
    with respx.mock:
        respx.get(OWNED_GAMES_URL).mock(
            return_value=httpx.Response(200, json=recorded("owned_games.json"))
        )
        assert (
            client.post(
                "/api/accounts",
                json={
                    "provider": "steam",
                    "external_account_id": STEAM_ID,
                    "label": "Main",
                    "credentials": API_KEY,
                },
            ).status_code
            == 201
        )
        assert client.post("/api/sync/steam").status_code == 200
    return client


def titles(body: dict[str, Any]) -> list[str]:
    return [work["title"] for work in body["works"]]


def test_a_synced_library_comes_back_whole(synced: TestClient) -> None:
    """The recorded fixture, through the sync, out of the read side."""

    body = synced.get("/api/works").json()

    assert titles(body) == LIBRARY
    assert body["next_cursor"] is None


def test_every_work_carries_its_platform_and_store_link(synced: TestClient) -> None:
    """The two things the table's platform column is made of.

    The link is built rather than stored: we never launch a game, so the store
    page is the answer to "where do I find this".
    """

    body = synced.get("/api/works").json()

    witcher = next(work for work in body["works"] if work["title"].startswith("The Witcher"))
    (entitlement,) = witcher["entitlements"]
    assert entitlement["provider"] == "steam"
    assert entitlement["provider_name"] == "Steam"
    assert entitlement["provider_item_id"] == "292030"
    assert entitlement["store_url"] == "https://store.steampowered.com/app/292030"


def test_a_stub_says_it_is_a_stub(synced: TestClient) -> None:
    """`is_matched` is false until M2 anchors the work to IGDB (ADR-0015)."""

    body = synced.get("/api/works").json()

    assert [work["is_matched"] for work in body["works"]] == [False, False, False]


def test_the_playtime_is_the_resolved_sum(synced: TestClient) -> None:
    """From `user_work_state`, not recomputed here: the resolver owns it (rule 5)."""

    body = synced.get("/api/works").json()

    witcher = next(work for work in body["works"] if work["title"].startswith("The Witcher"))
    assert witcher["playtime_minutes"] == 3247


def test_the_list_is_sorted_by_sort_title_not_title(synced: TestClient) -> None:
    """ "The Witcher 3" sorts under W. That is the whole reason the column exists."""

    body = synced.get("/api/works").json()

    assert [work["sort_title"] for work in body["works"]] == [
        "Dota 2",
        "Portal 2",
        "Witcher 3: Wild Hunt, The",
    ]


async def test_a_work_whose_last_copy_was_removed_leaves_the_list(
    synced: TestClient, session: AsyncSession
) -> None:
    """Rule 1 from the read side. The row is still there; the list is not the table."""

    entitlement = await session.scalar(
        select(Entitlement).where(Entitlement.provider_item_id == "620")
    )
    assert entitlement is not None
    entitlement.removed_at = utcnow()
    await session.commit()

    body = synced.get("/api/works").json()

    assert titles(body) == ["Dota 2", "The Witcher 3: Wild Hunt"]
    # Nothing was deleted, which is the point of marking rather than removing.
    assert await session.scalar(select(Work).where(Work.title == "Portal 2")) is not None


async def test_a_restored_copy_brings_its_work_back(
    synced: TestClient, session: AsyncSession
) -> None:
    entitlement = await session.scalar(
        select(Entitlement).where(Entitlement.provider_item_id == "620")
    )
    assert entitlement is not None
    entitlement.removed_at = utcnow()
    await session.commit()
    assert "Portal 2" not in titles(synced.get("/api/works").json())

    entitlement.removed_at = None
    await session.commit()

    assert "Portal 2" in titles(synced.get("/api/works").json())


async def test_a_removed_copy_is_not_listed_beside_a_live_one(
    synced: TestClient, session: AsyncSession
) -> None:
    """A work kept by one platform must not advertise the copy that went away.

    The `removed_at IS NULL` on the entitlement query, which is easy to put on
    the page query and forget here — and forgetting it shows up as a store link
    to something the user no longer owns.
    """

    live = await session.scalar(select(Entitlement).where(Entitlement.provider_item_id == "620"))
    assert live is not None
    gone = Entitlement(
        user_id=live.user_id,
        account_id=live.account_id,
        provider_item_id="620-elsewhere",
        provider_title="Portal 2 (a copy that went away)",
        removed_at=utcnow(),
    )
    session.add(gone)
    await session.flush()
    portal_id = await session.scalar(select(Work.id).where(Work.title == "Portal 2"))
    assert portal_id is not None
    session.add(
        EntitlementWork(entitlement_id=gone.id, work_id=portal_id, role=WorkLinkRole.GRANTED)
    )
    await session.commit()

    body = synced.get("/api/works").json()

    portal = next(work for work in body["works"] if work["title"] == "Portal 2")
    assert [item["provider_item_id"] for item in portal["entitlements"]] == ["620"]


def test_a_page_hands_back_a_cursor_and_the_next_page_continues(synced: TestClient) -> None:
    """Keyset, so the page is defined by content rather than by a count of rows."""

    first = synced.get("/api/works", params={"limit": 2}).json()
    assert titles(first) == LIBRARY[:2]
    assert first["next_cursor"]

    second = synced.get("/api/works", params={"limit": 2, "cursor": first["next_cursor"]}).json()

    assert titles(second) == LIBRARY[2:]
    assert second["next_cursor"] is None


def test_no_cursor_is_issued_for_a_page_that_ends_exactly(synced: TestClient) -> None:
    """Three of three: there is no next page, and offering one would be a lie."""

    body = synced.get("/api/works", params={"limit": 3}).json()

    assert titles(body) == LIBRARY
    assert body["next_cursor"] is None


def test_a_cursor_we_never_issued_is_refused(synced: TestClient) -> None:
    response = synced.get("/api/works", params={"cursor": "not-base64-at-all!!"})

    assert response.status_code == 400


def test_the_limit_is_bounded(synced: TestClient) -> None:
    """An unauthenticated caller cannot ask for one, but a signed-in one should not either."""

    assert synced.get("/api/works", params={"limit": 0}).status_code == 422
    assert synced.get("/api/works", params={"limit": 100_000}).status_code == 422


def test_an_empty_library_is_an_empty_page(client: TestClient) -> None:
    client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})

    body = client.get("/api/works").json()

    assert body == {"works": [], "next_cursor": None}


def test_the_listing_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/works").status_code == 401


async def test_another_users_library_is_not_in_the_list(
    synced: TestClient, session: AsyncSession
) -> None:
    """ADR-0003, on both queries this endpoint runs.

    A fixture with one user cannot tell a query that scopes by `user_id` from
    one that forgot to, which is how the same omission survived the first round
    of mutations on the sync endpoints.
    """

    stranger = AppUser(username="somebody-else", password_hash="not-a-hash")
    session.add(stranger)
    await session.flush()
    mine = await session.scalar(select(Entitlement).where(Entitlement.provider_item_id == "620"))
    assert mine is not None
    theirs = Entitlement(
        user_id=stranger.id,
        account_id=mine.account_id,
        provider_item_id="440",
        provider_title="Team Fortress 2",
    )
    session.add(theirs)
    await session.flush()
    theirs_work = Work(title="Team Fortress 2", sort_title="Team Fortress 2")
    session.add(theirs_work)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=theirs.id, work_id=theirs_work.id))
    session.add(UserWorkState(user_id=stranger.id, work_id=theirs_work.id))
    # And a second copy of one of mine, so the per-work query is scoped too.
    shared_id = await session.scalar(select(Work.id).where(Work.title == "Portal 2"))
    assert shared_id is not None
    session.add(
        EntitlementWork(
            entitlement_id=theirs.id,
            work_id=shared_id,
            role=WorkLinkRole.GRANTED,
        )
    )
    await session.commit()

    body = synced.get("/api/works").json()

    assert titles(body) == LIBRARY
    portal = next(work for work in body["works"] if work["title"] == "Portal 2")
    assert [item["provider_item_id"] for item in portal["entitlements"]] == ["620"]


async def test_a_copy_with_nothing_to_link_to_gets_no_link(
    synced: TestClient, session: AsyncSession
) -> None:
    """A manual entry has no `provider_item_id`, and `manual` has no store either.

    Null rather than a broken URL: a link to `.../app/` is worse than no link,
    because it looks like it should work.
    """

    manual = await session.scalar(select(Provider).where(Provider.key == "manual"))
    assert manual is not None
    account = Account(user_id=1, provider_id=manual.id, label="Discs")
    session.add(account)
    await session.flush()
    disc = Entitlement(
        user_id=1,
        account_id=account.id,
        origin=EntitlementOrigin.MANUAL,
        provider_title="Baldur's Gate II (disc)",
    )
    session.add(disc)
    await session.flush()
    work = Work(title="Baldur's Gate II", sort_title="Baldur's Gate II")
    session.add(work)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=disc.id, work_id=work.id))
    session.add(UserWorkState(user_id=1, work_id=work.id))
    await session.commit()

    body = synced.get("/api/works").json()

    gate = next(work for work in body["works"] if work["title"] == "Baldur's Gate II")
    (entry,) = gate["entitlements"]
    assert entry["provider_item_id"] is None
    assert entry["store_url"] is None


async def test_the_order_follows_sort_title_where_the_two_disagree(
    synced: TestClient, session: AsyncSession
) -> None:
    """The fixture's three titles sort the same either way, which proves nothing.

    "The Amazing Spider-Man" is the case that separates them: under `title` it
    lands with the Ts, under `sort_title` with the As — which is the entire
    reason `ludarium.titles` and the column exist.
    """

    await _add_work(session, title="The Amazing Spider-Man", sort_title="Amazing Spider-Man, The")

    body = synced.get("/api/works").json()

    assert titles(body) == ["The Amazing Spider-Man", *LIBRARY]


async def test_two_works_that_sort_alike_are_still_paged_apart(
    synced: TestClient, session: AsyncSession
) -> None:
    """The `id` half of the keyset. Without it a shared `sort_title` skips a row.

    Two remakes under one name is not a contrivance — it is what `sort_title`
    looks like before the matcher has told them apart, and the page boundary is
    exactly where the loss would be invisible.
    """

    first = await _add_work(session, title="Prey", sort_title="Prey")
    second = await _add_work(session, title="Prey", sort_title="Prey")

    page = synced.get("/api/works", params={"limit": 3}).json()
    assert titles(page) == ["Dota 2", "Portal 2", "Prey"]

    rest = synced.get("/api/works", params={"limit": 3, "cursor": page["next_cursor"]}).json()

    seen = [work["id"] for work in page["works"]] + [work["id"] for work in rest["works"]]
    assert first.id in seen
    assert second.id in seen
    assert len(seen) == len(set(seen))


async def test_the_order_ignores_case_and_trademark_signs(
    synced: TestClient, session: AsyncSession
) -> None:
    """Found by running M1 against a real library, where byte order ruled.

    "ARC Raiders" came ahead of "Amnesia", because `R` is a smaller byte than
    `m`; and one series filed as two blocks, because `:` is smaller than `™`.
    """

    for title in (
        "ARC Raiders",
        "Batman™: Arkham Knight",
        "Amnesia: The Dark Descent",
        "Batman: Arkham Asylum",
    ):
        await _add_work(session, title=title, sort_title=title)

    body = synced.get("/api/works").json()

    assert titles(body) == [
        "Amnesia: The Dark Descent",
        "ARC Raiders",
        "Batman: Arkham Asylum",
        "Batman™: Arkham Knight",
        *LIBRARY,
    ]


async def test_walking_the_cursor_meets_every_work_once_in_the_listed_order(
    synced: TestClient, session: AsyncSession
) -> None:
    """The ordering and the keyset have to be the same comparison, or a boundary loses rows.

    Walked one work at a time, so every adjacent pair is a page boundary once —
    including each pair that byte order and the folded key put the other way
    round, and the two that fold to one key and are told apart by id alone.
    """

    for title in ("ARC Raiders", "Amnesia", "arc raiders", "Batman™: Arkham Knight", "Batman: A"):
        await _add_work(session, title=title, sort_title=title)

    listed = [work["id"] for work in synced.get("/api/works").json()["works"]]

    walked: list[int] = []
    params: dict[str, str | int] = {"limit": 1}
    while True:
        page = synced.get("/api/works", params=params).json()
        walked += [work["id"] for work in page["works"]]
        if page["next_cursor"] is None:
            break
        params = {"limit": 1, "cursor": page["next_cursor"]}

    assert walked == listed


def test_a_cursor_issued_before_the_key_was_folded_is_refused(synced: TestClient) -> None:
    """`[sort_title, id]` is what every cursor was, and exactly the shape of `[key, id]`.

    Without the version it would be read as a position in an order it was never
    a position in, and the page would start somewhere nobody chose.
    """

    stale = urlsafe_b64encode(json.dumps(["Portal 2", 2]).encode()).decode()

    assert synced.get("/api/works", params={"cursor": stale}).status_code == 400


async def test_a_work_only_someone_else_still_owns_is_not_listed(
    synced: TestClient, session: AsyncSession
) -> None:
    """The `user_id` on the page query, which the `user_work_state` join hides.

    My copy was removed and theirs was not. The join keeps my own row for the
    work, so without the entitlement's `user_id` the work stays in my list on
    the strength of somebody else's copy — an empty-handed row that tells me a
    stranger owns something.
    """

    stranger = AppUser(username="somebody-else", password_hash="not-a-hash")
    session.add(stranger)
    await session.flush()
    mine = await session.scalar(select(Entitlement).where(Entitlement.provider_item_id == "620"))
    assert mine is not None
    portal_id = await session.scalar(select(Work.id).where(Work.title == "Portal 2"))
    assert portal_id is not None
    mine.removed_at = utcnow()
    theirs = Entitlement(
        user_id=stranger.id,
        account_id=mine.account_id,
        provider_item_id="620-theirs",
        provider_title="Portal 2",
    )
    session.add(theirs)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=theirs.id, work_id=portal_id))
    await session.commit()

    body = synced.get("/api/works").json()

    assert "Portal 2" not in titles(body)


async def _add_work(session: AsyncSession, *, title: str, sort_title: str) -> Work:
    """A work of mine, reachable the only way the listing accepts: a live entitlement."""

    account = await session.scalar(select(Account))
    assert account is not None
    work = Work(title=title, sort_title=sort_title)
    session.add(work)
    await session.flush()
    entitlement = Entitlement(
        user_id=1,
        account_id=account.id,
        provider_item_id=f"item-{work.id}",
        provider_title=title,
    )
    session.add(entitlement)
    await session.flush()
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    session.add(UserWorkState(user_id=1, work_id=work.id))
    await session.commit()
    return work


async def test_a_work_whose_state_row_is_missing_is_still_listed(
    synced: TestClient, session: AsyncSession
) -> None:
    """The convention `sync._stub` keeps, and that no constraint enforces.

    An inner join would make a future write path that forgets the row — a
    manual entry, the M2 matcher — delete games from the library with no error
    anywhere. Shown with its defaults instead: recoverable, and visible.
    """

    portal_id = await session.scalar(select(Work.id).where(Work.title == "Portal 2"))
    assert portal_id is not None
    state = await session.get(UserWorkState, (1, portal_id))
    assert state is not None
    await session.delete(state)
    await session.commit()

    body = synced.get("/api/works").json()

    portal = next(work for work in body["works"] if work["title"] == "Portal 2")
    assert portal["play_status"] == "not_started"
    assert portal["playtime_minutes"] == 0
    assert portal["is_hidden"] is False


@pytest.mark.parametrize(
    "cursor",
    [
        b'["Portal 2", 3.7]',
        b'["Portal 2", true]',
        b'[3, "Portal 2"]',
        b'["Portal 2"]',
        b'{"sort_title": "Portal 2", "id": 2}',
        b'"Portal 2"',
        b'[1, "portal 2", 2]',
        b'[2, "portal 2", 2.0]',
        b'[2, "portal 2", true]',
        b"[2, 3, 2]",
    ],
)
def test_a_cursor_of_the_wrong_shape_is_refused(synced: TestClient, cursor: bytes) -> None:
    """Checked rather than coerced.

    `str()` and `int()` accept almost anything, and `int(3.7)` becomes 3 without
    a word — so a made-up cursor would page from a position nobody chose, which
    is worse than being turned away.
    """

    response = synced.get("/api/works", params={"cursor": urlsafe_b64encode(cursor).decode()})

    assert response.status_code == 400


def test_a_cursor_longer_than_any_we_issue_is_refused(synced: TestClient) -> None:
    response = synced.get("/api/works", params={"cursor": "A" * 1000})

    assert response.status_code == 422


def test_a_work_whose_copies_vanished_between_the_two_queries_is_dropped(
    synced: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page and its entitlements are two queries and not one snapshot.

    pysqlite opens no transaction for a `SELECT`, so a sync committing a removal
    between them can leave a work in the page whose last live copy has just
    gone. Shown, it would be a row contradicting the endpoint's own rule — in
    the list because something live points at it, with nothing listed.

    The race is forced here rather than waited for: what the second query
    returns is the whole of the difference.
    """

    real = works_module._entitlements

    async def loses_portal(
        session: AsyncSession, work_ids: list[int], user_id: int
    ) -> dict[int, list[object]]:
        found = await real(session, work_ids, user_id)
        for work_id, copies in list(found.items()):
            if any(copy.provider_item_id == "620" for copy in copies):
                del found[work_id]
        return found

    monkeypatch.setattr(works_module, "_entitlements", loses_portal)

    first = synced.get("/api/works", params={"limit": 2}).json()

    assert titles(first) == ["Dota 2"]
    # The cursor is a position in the ordering, taken from the last row *read*.
    # Issued from the last row kept, the next page would start before Portal 2
    # and hand it back again — the row just established as having nothing live.
    second = synced.get("/api/works", params={"cursor": first["next_cursor"]}).json()
    assert titles(second) == ["The Witcher 3: Wild Hunt"]


def test_a_page_that_loses_everything_still_hands_back_a_cursor(
    synced: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the listing stops and the rest of the library is never asked for.

    The cursor is a position, not a row: taken from the last row *kept* there
    would be none to take it from here, and a client doing the obvious thing —
    stop when the cursor is null — would silently see a truncated library.
    """

    async def loses_everything(*args: object, **kwargs: object) -> dict[int, list[object]]:
        return {}

    monkeypatch.setattr(works_module, "_entitlements", loses_everything)

    body = synced.get("/api/works", params={"limit": 1}).json()

    assert body["works"] == []
    assert body["next_cursor"] is not None


async def scored(session: AsyncSession, title: str, score: int | None, slug: str | None) -> None:
    work = await session.scalar(select(Work).where(Work.title == title))
    assert work is not None
    work.metacritic_score = score
    if slug is not None:
        session.add(
            ExternalId(entity_type=EntityType.WORK, entity_id=work.id, namespace="rawg", value=slug)
        )
    await session.commit()


def metacritic(client: TestClient, title: str) -> Any:
    body = client.get("/api/works").json()
    return next(work for work in body["works"] if work["title"] == title)["metacritic"]


async def test_a_score_comes_with_the_rawg_page_it_is_credited_to(
    synced: TestClient, session: AsyncSession
) -> None:
    await scored(session, "Portal 2", 95, "portal-2")

    assert metacritic(synced, "Portal 2") == {
        "value": 95,
        "source_name": "RAWG",
        "source_url": "https://rawg.io/games/portal-2",
    }
    assert metacritic(synced, "Dota 2") is None


async def test_a_score_with_nothing_to_credit_it_to_is_not_served(
    synced: TestClient, session: AsyncSession
) -> None:
    """RAWG's terms: an active link wherever its data is shown. No link, no score."""

    await scored(session, "Portal 2", 95, None)

    assert metacritic(synced, "Portal 2") is None


async def test_two_slugs_for_one_work_do_not_list_it_twice(
    synced: TestClient, session: AsyncSession
) -> None:
    await scored(session, "Portal 2", 95, "portal-2-old")
    await scored(session, "Portal 2", 95, "portal-2")

    body = synced.get("/api/works").json()

    assert titles(body) == LIBRARY
    assert metacritic(synced, "Portal 2")["source_url"] == "https://rawg.io/games/portal-2"


async def reviewed(session: AsyncSession, title: str, appid: str | None = "620") -> None:
    work = await session.scalar(select(Work).where(Work.title == title))
    assert work is not None
    work.steam_review_rating = SteamRating.OVERWHELMINGLY_POSITIVE
    work.steam_review_percent = 98
    work.steam_review_count = 390695
    work.steam_review_appid = appid
    await session.commit()


def steam_reviews(client: TestClient, title: str) -> Any:
    body = client.get("/api/works").json()
    return next(work for work in body["works"] if work["title"] == title)["steam_reviews"]


async def test_a_steam_score_links_to_the_reviews_on_its_store_page(
    synced: TestClient, session: AsyncSession
) -> None:
    await reviewed(session, "Portal 2")

    assert steam_reviews(synced, "Portal 2") == {
        "rating": "overwhelmingly_positive",
        "percent": 98,
        "count": 390695,
        "url": "https://store.steampowered.com/app/620#app_reviews_hash",
    }
    assert steam_reviews(synced, "Dota 2") is None


async def test_a_steam_score_that_names_no_app_is_not_served(
    synced: TestClient, session: AsyncSession
) -> None:
    """The four columns are one verdict about one app; without the app it is nobody's."""

    await reviewed(session, "Portal 2", appid=None)

    assert steam_reviews(synced, "Portal 2") is None


async def test_a_steam_score_without_a_verdict_is_served_with_its_count(
    synced: TestClient, session: AsyncSession
) -> None:
    """Too few reviews for a verdict: the percentage stands, and the count says how few."""

    await reviewed(session, "Portal 2")
    work = await session.scalar(select(Work).where(Work.title == "Portal 2"))
    assert work is not None
    work.steam_review_rating, work.steam_review_percent, work.steam_review_count = None, 80, 5
    await session.commit()

    assert steam_reviews(synced, "Portal 2") == {
        "rating": None,
        "percent": 80,
        "count": 5,
        "url": "https://store.steampowered.com/app/620#app_reviews_hash",
    }


async def covered(
    client: TestClient, session: AsyncSession, title: str, *files: tuple[int, int, bytes | None]
) -> list[int]:
    """Cover rows for a work, one per (width, height, bytes); None bytes is a row with no file."""

    work = await session.scalar(select(Work).where(Work.title == title))
    assert work is not None
    data_dir = client.app.state.settings.data_dir  # type: ignore[attr-defined]
    ids = []
    for width, height, body in files:
        relative = f"covers/igdb/co{width}.jpg"
        if body is not None:
            (data_dir / "covers" / "igdb").mkdir(parents=True, exist_ok=True)
            (data_dir / relative).write_bytes(body)
        image = ImageAsset(
            entity_type=EntityType.WORK,
            entity_id=work.id,
            kind=ImageKind.COVER,
            source_ref="igdb",
            local_path=relative,
            width=width,
            height=height,
            fetched_at=utcnow(),
        )
        session.add(image)
        await session.flush()
        ids.append(image.id)
    await session.commit()
    return ids


JPEG = b"\xff\xd8\xff\xe0 a cover"


async def test_a_cover_is_listed_at_both_sizes_and_served_from_disk(
    synced: TestClient, session: AsyncSession
) -> None:
    small, sharp = await covered(synced, session, "Portal 2", (264, 374, JPEG), (528, 748, JPEG))

    body = synced.get("/api/works").json()
    portal = next(work for work in body["works"] if work["title"] == "Portal 2")
    assert portal["cover"] == {
        "url": f"/api/images/{small}",
        "url_2x": f"/api/images/{sharp}",
        "width": 264,
        "height": 374,
    }
    dota = next(work for work in body["works"] if work["title"] == "Dota 2")
    assert dota["cover"] is None

    served = synced.get(portal["cover"]["url"])
    assert served.status_code == 200
    assert served.content == JPEG
    assert served.headers["content-type"] == "image/jpeg"
    assert "immutable" in served.headers["cache-control"]


async def test_an_image_whose_file_is_gone_is_a_404(
    synced: TestClient, session: AsyncSession
) -> None:
    (gone,) = await covered(synced, session, "Portal 2", (264, 374, None))

    assert synced.get(f"/api/images/{gone}").status_code == 404
    assert synced.get("/api/images/99999").status_code == 404


async def test_a_row_pointing_outside_the_data_directory_is_not_served(
    synced: TestClient, session: AsyncSession, tmp_path: Path
) -> None:
    (image_id,) = await covered(synced, session, "Portal 2", (264, 374, JPEG))
    secret = tmp_path / "secret.txt"
    secret.write_text("not an image")
    image = await session.get_one(ImageAsset, image_id)
    image.local_path = "../secret.txt"
    await session.commit()

    assert synced.get(f"/api/images/{image_id}").status_code == 404


def test_an_image_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/images/1").status_code == 401


async def retitled(session: AsyncSession, old: str, new: str) -> None:
    """What the resolver does when IGDB names a work: `title` changes, the store's name does not."""

    work = await session.scalar(select(Work).where(Work.title == old))
    assert work is not None
    work.title = new
    await session.commit()


def search(client: TestClient, q: str, **params: Any) -> dict[str, Any]:
    response = client.get("/api/works", params={"q": q, **params})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_a_search_finds_a_title_by_any_part_of_it_whatever_the_case(synced: TestClient) -> None:
    assert titles(search(synced, "ITCHER")) == ["The Witcher 3: Wild Hunt"]
    assert titles(search(synced, "wild hunt")) == ["The Witcher 3: Wild Hunt"]


def test_a_blank_search_is_no_search(synced: TestClient) -> None:
    assert titles(search(synced, "   ")) == LIBRARY


async def test_a_search_folds_as_the_order_does(synced: TestClient, session: AsyncSession) -> None:
    """Accents and trademark signs, the fold #40 orders by, match either way round."""

    await retitled(session, "Dota 2", "Brütal Legend™")

    assert titles(search(synced, "brutal legend")) == ["Brütal Legend™"]
    assert titles(search(synced, "BRÜTAL")) == ["Brütal Legend™"]


async def test_the_name_the_store_gives_a_game_finds_it(
    synced: TestClient, session: AsyncSession
) -> None:
    """Rule 5 keeps the store's name apart from the work's, and search reads both."""

    await retitled(session, "Portal 2", "Portal Two")

    assert titles(search(synced, "portal 2")) == ["Portal Two"]


def test_a_wildcard_in_a_search_is_a_character_to_find(synced: TestClient) -> None:
    assert titles(search(synced, "%")) == []
    assert titles(search(synced, "_")) == []


def test_a_search_pages_on_the_same_cursor_as_the_library(synced: TestClient) -> None:
    first = search(synced, "2", limit=1)
    assert titles(first) == ["Dota 2"]

    second = search(synced, "2", limit=1, cursor=first["next_cursor"])

    assert titles(second) == ["Portal 2"]
    assert second["next_cursor"] is None


def test_a_search_longer_than_any_title_is_refused(synced: TestClient) -> None:
    assert synced.get("/api/works", params={"q": "x" * 201}).status_code == 422


def work_id(client: TestClient, title: str) -> int:
    body = client.get("/api/works").json()
    found: int = next(work["id"] for work in body["works"] if work["title"] == title)
    return found


async def test_a_work_s_page_has_what_its_card_has_and_what_the_grid_leaves_out(
    synced: TestClient, session: AsyncSession
) -> None:
    work = await session.scalar(select(Work).where(Work.title == "Portal 2"))
    assert work is not None
    work.summary = "Test subjects and portals."
    work.release_date = date(2011, 4, 18)
    work.release_year = 2011
    await session.commit()
    card = next(
        entry for entry in synced.get("/api/works").json()["works"] if entry["id"] == work.id
    )

    body = synced.get(f"/api/works/{work.id}").json()

    # The card's fields, unchanged: one description of a work, not two.
    assert {key: body[key] for key in card} == card
    assert body["summary"] == "Test subjects and portals."
    assert body["release_date"] == "2011-04-18"
    assert body["companies"] == []
    # Per copy and summed, as rule 5's exception describes.
    witcher = synced.get(f"/api/works/{work_id(synced, 'The Witcher 3: Wild Hunt')}").json()
    per_copy = [copy["playtime_minutes"] for copy in witcher["entitlements"]]
    assert per_copy == [3247]
    assert witcher["playtime_minutes"] == 3247


async def test_credits_name_each_company_once_publishers_first(
    synced: TestClient, session: AsyncSession
) -> None:
    portal = work_id(synced, "Portal 2")
    valve, porter = Company(name="Valve"), Company(name="another port house")
    session.add_all([valve, porter])
    await session.flush()
    session.add_all(
        [
            WorkCompany(work_id=portal, company_id=porter.id, role=CompanyRole.PORTING),
            WorkCompany(work_id=portal, company_id=valve.id, role=CompanyRole.DEVELOPER),
            WorkCompany(work_id=portal, company_id=valve.id, role=CompanyRole.PUBLISHER),
        ]
    )
    await session.commit()

    body = synced.get(f"/api/works/{portal}").json()

    assert body["companies"] == [
        {"name": "Valve", "roles": ["publisher", "developer"]},
        {"name": "another port house", "roles": ["porting"]},
    ]


def test_a_work_that_does_not_exist_is_a_404(synced: TestClient) -> None:
    assert synced.get("/api/works/999999").status_code == 404


async def test_a_work_only_removed_copies_point_at_is_a_404_too(
    synced: TestClient, session: AsyncSession
) -> None:
    """The listing does not show it, so neither does its page (rule 1 from the read side)."""

    portal = work_id(synced, "Portal 2")
    entitlement = await session.scalar(
        select(Entitlement).where(Entitlement.provider_item_id == "620")
    )
    assert entitlement is not None
    entitlement.removed_at = utcnow()
    await session.commit()

    response = synced.get(f"/api/works/{portal}")

    assert response.status_code == 404
    # The same answer as for a work that never existed.
    assert response.json() == synced.get("/api/works/999999").json()


def test_a_work_s_page_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/works/1").status_code == 401
