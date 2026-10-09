from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api import imports as imports_api
from ludarium.enums import EntitlementOrigin, EntityType, SourceKind, SyncTrigger
from ludarium.models import Account, Entitlement, FieldProvenance, Provider

LIBRARY = (
    b"title,platform,playtime_minutes,release_year\n"
    b"Mass Effect,Origin,1200,2007\n"
    b"Doom,Shelf,,1993\n"
    b"Doom,Shelf,,1993\n"
    b"Portal 2,Steam,300,\n"
)


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    response = client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return client


def upload(
    client: TestClient,
    data: bytes,
    *,
    path: str = "/api/import",
    name: str = "games.csv",
    sweep: bool = False,
    expected: int = 200,
) -> dict[str, Any]:
    response = client.post(
        path, files={"file": (name, data, "text/csv")}, data={"sweep": str(sweep).lower()}
    )
    assert response.status_code == expected, response.text
    answer: dict[str, Any] = response.json()
    return answer


def preview(client: TestClient, data: bytes, expected: int = 200) -> dict[str, Any]:
    return upload(client, data, path="/api/import/preview", expected=expected)


def by_label(answer: dict[str, Any], key: str) -> dict[str, Any]:
    return {entry["label"]: entry for entry in answer[key]}


async def connect_steam(session: AsyncSession) -> None:
    steam = await session.scalar(select(Provider).where(Provider.key == "steam"))
    assert steam is not None
    session.add(
        Account(user_id=1, provider_id=steam.id, external_account_id="7656119", label="Main")
    )
    await session.commit()


async def test_a_preview_says_where_each_row_goes_and_writes_nothing(
    signed_in: TestClient, session: AsyncSession
) -> None:
    answer = preview(signed_in, LIBRARY)

    assert (answer["format"], answer["rows_read"], answer["problem_count"]) == ("csv", 4, 0)
    assert answer["sample"][0] == {"row": 2, "title": "Mass Effect", "platform": "Origin"}
    groups = by_label(answer, "groups")
    assert {label: (g["provider"], g["items"], g["status"]) for label, g in groups.items()} == {
        "Origin": ("ea", 1, "new"),
        "Shelf": ("other", 2, "new"),
        # No Steam account here, so the file is the only word on it.
        "Steam": ("steam", 1, "new"),
    }
    assert groups["Origin"]["provider_name"] == "EA app"
    assert answer["can_sweep"] is True
    assert await session.scalar(select(func.count()).select_from(Account)) == 0


async def test_an_import_files_each_platform_into_a_derived_account(
    signed_in: TestClient, session: AsyncSession
) -> None:
    answer = upload(signed_in, LIBRARY)

    outcomes = by_label(answer, "outcomes")
    assert {label: o["run"]["items_added"] for label, o in outcomes.items()} == {
        "Origin": 1,
        "Shelf": 2,
        "Steam": 1,
    }
    run = outcomes["Origin"]["run"]
    assert (run["provider"], run["trigger"], run["status"]) == ("manual", "import", "success")
    accounts = (await session.scalars(select(Account).order_by(Account.id))).all()
    assert all(account.is_derived for account in accounts)
    origins = set(await session.scalars(select(Entitlement.origin)))
    assert origins == {EntitlementOrigin.IMPORT}
    # The user's own list, at the rung rule 5 gives the user.
    kinds = set(
        await session.scalars(
            select(FieldProvenance.source_kind).where(
                FieldProvenance.entity_type == EntityType.WORK,
                FieldProvenance.field == "release_year",
            )
        )
    )
    assert kinds == {SourceKind.MANUAL}
    await session.commit()
    works = signed_in.get("/api/works").json()["works"]
    assert sorted((work["title"], work["release_year"]) for work in works) == [
        ("Doom", 1993),
        ("Doom", 1993),
        ("Mass Effect", 2007),
        ("Portal 2", None),
    ]


def test_importing_the_same_file_again_updates_rather_than_duplicates(
    signed_in: TestClient,
) -> None:
    upload(signed_in, LIBRARY)

    again = by_label(upload(signed_in, LIBRARY), "outcomes")

    assert {
        label: (o["run"]["items_added"], o["run"]["items_updated"]) for label, o in again.items()
    } == {
        "Origin": (0, 1),
        "Shelf": (0, 2),
        "Steam": (0, 1),
    }
    assert len(signed_in.get("/api/works").json()["works"]) == 4


async def test_rows_for_a_connected_platform_are_left_to_its_sync(
    signed_in: TestClient, session: AsyncSession
) -> None:
    await connect_steam(session)

    groups = by_label(preview(signed_in, LIBRARY), "groups")
    outcomes = by_label(upload(signed_in, LIBRARY), "outcomes")

    assert groups["Steam"]["status"] == "connected"
    assert (outcomes["Steam"]["status"], outcomes["Steam"]["run"]) == ("connected", None)
    assert outcomes["Origin"]["run"]["status"] == "success"
    steam_copies = await session.scalar(
        select(func.count())
        .select_from(Entitlement)
        .join(Account, Account.id == Entitlement.account_id)
        .join(Provider, Provider.id == Account.provider_id)
        .where(Provider.key == "steam")
    )
    assert steam_copies == 0


def test_a_shorter_file_removes_nothing_unless_asked(signed_in: TestClient) -> None:
    upload(signed_in, LIBRARY)
    shorter = b"title,platform\nDoom,Shelf\n"

    shelf = by_label(preview(signed_in, shorter), "groups")["Shelf"]
    kept = by_label(upload(signed_in, shorter), "outcomes")["Shelf"]["run"]

    # The second Doom and Origin's and Steam's games are not in this file.
    assert (shelf["status"], shelf["would_remove"]) == ("existing", 1)
    assert kept["items_removed"] == 0

    swept = by_label(upload(signed_in, shorter, sweep=True), "outcomes")["Shelf"]["run"]

    assert swept["items_removed"] == 1
    # Only the accounts the file names: Origin's game is still owned.
    titles = sorted(work["title"] for work in signed_in.get("/api/works").json()["works"])
    assert titles == ["Doom", "Mass Effect", "Portal 2"]


def test_a_file_with_unread_rows_cannot_remove(signed_in: TestClient) -> None:
    damaged = LIBRARY + b"Risen,Shelf,lots,\n"

    answer = preview(signed_in, damaged)
    refused = upload(signed_in, damaged, sweep=True, expected=422)

    assert answer["can_sweep"] is False
    assert answer["problems"] == [
        {"row": 6, "message": "`playtime_minutes` is 'lots', not a whole number"}
    ]
    assert "cannot remove" in refused["detail"]
    # Without a sweep the readable rows import.
    assert by_label(upload(signed_in, damaged), "outcomes")["Shelf"]["run"]["items_added"] == 2


async def test_a_switched_off_account_takes_nothing(
    signed_in: TestClient, session: AsyncSession
) -> None:
    upload(signed_in, LIBRARY)
    shelf = await session.scalar(select(Account).where(Account.label == "Shelf"))
    assert shelf is not None
    await session.commit()
    signed_in.patch(f"/api/accounts/{shelf.id}", json={"is_active": False})

    outcome = by_label(upload(signed_in, LIBRARY), "outcomes")["Shelf"]

    assert (outcome["status"], outcome["run"]) == ("switched_off", None)


def test_an_unreadable_file_is_refused_with_why(signed_in: TestClient) -> None:
    answer = preview(signed_in, b"name\nGothic\n", expected=422)

    assert "`title` column is required" in answer["detail"]


def test_an_upload_past_the_bound_is_refused_before_it_is_read(signed_in: TestClient) -> None:
    response = signed_in.post(
        "/api/import/preview",
        files={"file": ("a.csv", b"x" * (imports_api.UPLOAD_BYTES + 1), "text/csv")},
    )

    assert response.status_code == 413


def test_import_needs_a_session(client: TestClient) -> None:
    assert client.post("/api/import/preview", files={"file": ("a.csv", LIBRARY)}).status_code == 401


def test_a_run_is_recorded_as_an_import(signed_in: TestClient) -> None:
    upload(signed_in, LIBRARY)

    triggers = {run["trigger"] for run in signed_in.get("/api/sync/runs").json()["runs"]}

    assert triggers == {SyncTrigger.IMPORT.value}


async def test_a_file_naming_too_many_platforms_creates_no_account(
    signed_in: TestClient, session: AsyncSession
) -> None:
    rows = "".join(f"Game {n},Shelf {n}\n" for n in range(100))

    answer = upload(signed_in, ("title,platform\n" + rows).encode(), expected=422)

    assert "each would be an account" in answer["detail"]
    assert await session.scalar(select(func.count()).select_from(Account)) == 0


async def test_connecting_a_platform_lets_a_sweep_retire_what_an_import_put_there(
    signed_in: TestClient, session: AsyncSession
) -> None:
    upload(signed_in, LIBRARY)
    await connect_steam(session)

    steam = by_label(preview(signed_in, LIBRARY), "groups")["Steam"]
    kept = by_label(upload(signed_in, LIBRARY), "outcomes")["Steam"]
    retired = by_label(upload(signed_in, LIBRARY, sweep=True), "outcomes")["Steam"]

    assert (steam["status"], steam["would_remove"]) == ("connected", 1)
    assert kept["run"] is None
    assert (retired["status"], retired["run"]["items_removed"]) == ("connected", 1)
    removed = signed_in.get("/api/entitlements/removed").json()
    assert [entry["provider_title"] for entry in removed] == ["Portal 2"]
