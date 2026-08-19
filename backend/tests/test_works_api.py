import json
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api import works as works_module
from ludarium.enums import EntitlementOrigin, WorkLinkRole
from ludarium.models import (
    Account,
    AppUser,
    Entitlement,
    EntitlementWork,
    Provider,
    UserWorkState,
    Work,
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
