from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_account, make_user
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_sync import THREE_GAMES, FakeLibrary

from ludarium.enums import EntitlementOrigin, EntityType, SourceKind, WorkLinkRole
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    FieldProvenance,
    Provider,
    Work,
)
from ludarium.resolver import record, resolve
from ludarium.sync import sync_account

DISC = {
    "title": "  Baldur's Gate II  ",
    "store_label": "Big box, CD",
    "ownership_type": "physical",
    "item_kind": "game",
    "release_year": 2000,
}


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    response = client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return client


def add(client: TestClient, **changes: Any) -> dict[str, Any]:
    response = client.post("/api/entitlements/manual", json=DISC | changes)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def works(client: TestClient, query: str = "") -> list[dict[str, Any]]:
    response = client.get(f"/api/works{query}")
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()["works"]
    return listed


async def provenance(session: AsyncSession, entity_type: EntityType, entity_id: int) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(FieldProvenance)
        .where(FieldProvenance.entity_type == entity_type, FieldProvenance.entity_id == entity_id)
    )
    await session.commit()
    return count or 0


def test_an_entry_is_a_copy_in_the_library_like_any_other(signed_in: TestClient) -> None:
    entry = add(signed_in)

    assert entry == {
        "id": entry["id"],
        "work_id": entry["work_id"],
        "title": "Baldur's Gate II",
        "store_label": "Big box, CD",
        "ownership_type": "physical",
        "item_kind": "game",
        "release_year": 2000,
    }
    [work] = works(signed_in)
    assert (work["id"], work["title"], work["release_year"], work["item_kind"]) == (
        entry["work_id"],
        "Baldur's Gate II",
        2000,
        "game",
    )
    assert work["is_matched"] is False
    [copy] = work["entitlements"]
    assert (copy["provider"], copy["provider_title"], copy["store_label"]) == (
        "manual",
        "Baldur's Gate II",
        "Big box, CD",
    )
    assert (copy["provider_item_id"], copy["store_url"], copy["kept"]) == (None, None, False)


def test_the_filters_find_an_entry(signed_in: TestClient) -> None:
    """The issue's third line: in the filters like any other copy."""

    add(signed_in)

    assert [work["title"] for work in works(signed_in, "?platform=manual")] == ["Baldur's Gate II"]
    assert [work["title"] for work in works(signed_in, "?kind=game")] == ["Baldur's Gate II"]
    assert [work["title"] for work in works(signed_in, "?year_min=2000&year_max=2000")] == [
        "Baldur's Gate II"
    ]
    assert works(signed_in, "?platform=steam") == []
    assert [work["title"] for work in works(signed_in, "?q=baldur")] == ["Baldur's Gate II"]


async def test_every_entry_goes_on_one_manual_account(
    signed_in: TestClient, session: AsyncSession
) -> None:
    add(signed_in)
    add(signed_in, title="Planescape: Torment", store_label="GOG key")

    manual = await session.scalar(select(Provider).where(Provider.key == "manual"))
    assert manual is not None
    accounts = list(await session.scalars(select(Account).where(Account.provider_id == manual.id)))
    await session.commit()
    assert [(account.label, account.credentials_encrypted) for account in accounts] == [
        ("Manual entry", None)
    ]
    assert len(works(signed_in)) == 2


async def test_what_the_user_typed_is_manual_provenance(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """Rule 5: so that it outranks every other source."""

    entry = add(signed_in)

    rows = list(
        await session.scalars(
            select(FieldProvenance).where(FieldProvenance.source_kind == SourceKind.MANUAL)
        )
    )
    await session.commit()
    assert sorted((row.entity_type, row.field, row.value, row.source_ref) for row in rows) == [
        (EntityType.ENTITLEMENT, "provider_title", "Baldur's Gate II", "manual"),
        (EntityType.WORK, "item_kind", "game", "manual"),
        (EntityType.WORK, "release_year", 2000, "manual"),
    ]
    assert all(row.is_effective for row in rows)
    assert {row.entity_id for row in rows if row.entity_type is EntityType.WORK} == {
        entry["work_id"]
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "   "},
        {"title": "x" * 301},
        {"release_year": 1949},
        {"release_year": 2101},
        {"ownership_type": "stolen"},
        {"store_label": "x" * 101},
        {"provider_item_id": "292030"},
    ],
)
def test_an_entry_that_is_not_one_is_refused(
    signed_in: TestClient, changes: dict[str, Any]
) -> None:
    response = signed_in.post("/api/entitlements/manual", json=DISC | changes)

    assert response.status_code == 422, response.text
    assert works(signed_in) == []


def test_a_blank_label_and_a_missing_year_are_none(signed_in: TestClient) -> None:
    entry = add(signed_in, store_label="  ", release_year=None)

    assert (entry["store_label"], entry["release_year"]) == (None, None)


def test_an_entry_reads_back_as_it_was_written(signed_in: TestClient) -> None:
    entry = add(signed_in)

    response = signed_in.get(f"/api/entitlements/manual/{entry['id']}")

    assert response.status_code == 200
    assert response.json() == entry


async def test_an_edit_replaces_the_entry_and_renames_its_stub(
    signed_in: TestClient, session: AsyncSession
) -> None:
    entry = add(signed_in)
    edited = DISC | {
        "title": "Baldur's Gate II: Shadows of Amn",
        "store_label": None,
        "ownership_type": "owned",
        "release_year": 2001,
    }

    response = signed_in.put(f"/api/entitlements/manual/{entry['id']}", json=edited)

    assert response.status_code == 200, response.text
    assert response.json() == entry | {
        "title": "Baldur's Gate II: Shadows of Amn",
        "store_label": None,
        "ownership_type": "owned",
        "release_year": 2001,
    }
    [work] = works(signed_in, "?q=shadows")
    assert (work["title"], work["release_year"]) == ("Baldur's Gate II: Shadows of Amn", 2001)
    assert work["entitlements"][0]["provider_title"] == "Baldur's Gate II: Shadows of Amn"
    stored = await session.get_one(Entitlement, entry["id"])
    await session.commit()
    assert stored.ownership_type == "owned"


async def test_clearing_the_year_lets_the_matched_year_show_through(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """ADR-0031: "I gave no year" is a withdrawal, not a null that outranks IGDB's."""

    entry = add(signed_in)
    work = await session.get_one(Work, entry["work_id"])
    work.is_matched = True
    work.title = "Baldur's Gate II: Shadows of Amn"
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field="release_year",
        source_kind=SourceKind.METADATA_PROVIDER,
        source_ref="igdb",
        value=2000,
    )
    await resolve(session, entity_type=EntityType.WORK, entity_id=work.id, fields=["release_year"])
    await session.commit()

    year = signed_in.put(
        f"/api/entitlements/manual/{entry['id']}", json=DISC | {"release_year": 1999}
    )
    assert [(work["title"], work["release_year"]) for work in works(signed_in)] == [
        # A matched work keeps IGDB's title: the disc's name is the copy's.
        ("Baldur's Gate II: Shadows of Amn", 1999)
    ]
    cleared = signed_in.put(
        f"/api/entitlements/manual/{entry['id']}", json=DISC | {"release_year": None}
    )

    assert (year.status_code, cleared.status_code) == (200, 200)
    assert cleared.json()["release_year"] is None
    assert [work["release_year"] for work in works(signed_in)] == [2000]


def test_clearing_the_only_year_leaves_none(signed_in: TestClient) -> None:
    entry = add(signed_in)

    signed_in.put(f"/api/entitlements/manual/{entry['id']}", json=DISC | {"release_year": None})

    assert [work["release_year"] for work in works(signed_in)] == [None]


async def test_a_delete_takes_the_entry_and_its_stub_with_it(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """ADR-0031: a `DELETE`, and nothing for the removed view to offer back."""

    entry = add(signed_in)
    assert signed_in.patch(f"/api/works/{entry['work_id']}/state", json={"rating": 8}).is_success

    response = signed_in.delete(f"/api/entitlements/manual/{entry['id']}")

    assert response.status_code == 204
    assert works(signed_in) == []
    assert signed_in.get("/api/entitlements/removed").json() == []
    assert signed_in.get(f"/api/entitlements/manual/{entry['id']}").status_code == 404
    assert await session.get(Entitlement, entry["id"]) is None
    assert await session.get(Work, entry["work_id"]) is None
    assert await provenance(session, EntityType.ENTITLEMENT, entry["id"]) == 0
    assert await provenance(session, EntityType.WORK, entry["work_id"]) == 0


async def test_a_delete_leaves_a_matched_work_alone(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """Another copy may reach it after a merge; whether it goes is the orphan collector's call."""

    entry = add(signed_in)
    work = await session.get_one(Work, entry["work_id"])
    work.is_matched = True
    await session.commit()

    assert signed_in.delete(f"/api/entitlements/manual/{entry['id']}").status_code == 204

    # Asked of the database: the session still holds the work it loaded above.
    assert await session.scalar(select(Entitlement.id).where(Entitlement.id == entry["id"])) is None
    assert await session.scalar(select(Work.id).where(Work.id == entry["work_id"])) is not None
    await session.commit()


async def test_a_kept_work_forgets_what_the_deleted_entry_said(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """The year and kind were the entry's. Left behind, they would outrank IGDB with no
    form left to change them (rule 3 for a user who has taken their word back)."""

    entry = add(signed_in)
    work = await session.get_one(Work, entry["work_id"])
    work.is_matched = True
    await record(
        session,
        entity_type=EntityType.WORK,
        entity_id=work.id,
        field="release_year",
        source_kind=SourceKind.METADATA_PROVIDER,
        source_ref="igdb",
        value=2001,
    )
    await session.commit()

    assert signed_in.delete(f"/api/entitlements/manual/{entry['id']}").status_code == 204

    manual = await session.scalar(
        select(func.count())
        .select_from(FieldProvenance)
        .where(
            FieldProvenance.entity_type == EntityType.WORK,
            FieldProvenance.entity_id == entry["work_id"],
            FieldProvenance.source_kind == SourceKind.MANUAL,
        )
    )
    row = (
        await session.execute(
            select(Work.release_year, Work.item_kind).where(Work.id == entry["work_id"])
        )
    ).one()
    await session.commit()
    assert manual == 0
    assert tuple(row) == (2001, None)


async def test_a_synced_copy_is_not_a_manual_entry(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """Rule 2 from the other side: this door is for what no platform reports."""

    account = await make_account(session)
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    synced = await session.scalar(select(Entitlement.id).limit(1))
    await session.commit()

    assert signed_in.get(f"/api/entitlements/manual/{synced}").status_code == 404
    assert signed_in.put(f"/api/entitlements/manual/{synced}", json=DISC).status_code == 404
    assert signed_in.delete(f"/api/entitlements/manual/{synced}").status_code == 404
    assert len(works(signed_in)) == 3


async def test_another_users_entry_is_not_there(
    signed_in: TestClient, session: AsyncSession
) -> None:
    entry = add(signed_in)
    other = await make_user(session, username="someone-else")
    stored = await session.get_one(Entitlement, entry["id"])
    stored.user_id = other.id
    await session.commit()

    assert signed_in.get(f"/api/entitlements/manual/{entry['id']}").status_code == 404
    assert signed_in.delete(f"/api/entitlements/manual/{entry['id']}").status_code == 404


async def test_a_sync_after_a_manual_add_leaves_the_entry_untouched(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """Rule 2, end to end: a disc no platform knows about survives a run that lists nothing."""

    entry = add(signed_in)
    account = await make_account(session)
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    before = await provenance(session, EntityType.ENTITLEMENT, entry["id"])

    await sync_account(session, account=account, library=FakeLibrary([]))

    stored = await session.get_one(Entitlement, entry["id"])
    await session.commit()
    assert (stored.origin, stored.provider_title, stored.removed_at, stored.kept_at) == (
        EntitlementOrigin.MANUAL,
        "Baldur's Gate II",
        None,
        None,
    )
    assert await provenance(session, EntityType.ENTITLEMENT, entry["id"]) == before
    assert signed_in.get(f"/api/entitlements/manual/{entry['id']}").json() == entry
    assert [work["title"] for work in works(signed_in)] == ["Baldur's Gate II"]


def test_an_entry_needs_a_session(client: TestClient) -> None:
    assert client.post("/api/entitlements/manual", json=DISC).status_code == 401


async def test_a_delete_keeps_what_another_entry_on_the_work_said(
    signed_in: TestClient, session: AsyncSession
) -> None:
    """The work-level rows are keyed by field, not by entry: the other copy's form shows them."""

    first = add(signed_in)
    second = add(signed_in, title="Baldur's Gate II (second disc)")
    work = await session.get_one(Work, first["work_id"])
    work.is_matched = True
    session.add(
        EntitlementWork(
            entitlement_id=second["id"], work_id=first["work_id"], role=WorkLinkRole.GRANTED
        )
    )
    await session.commit()

    assert signed_in.delete(f"/api/entitlements/manual/{first['id']}").status_code == 204

    assert await provenance(session, EntityType.WORK, first["work_id"]) == 2
