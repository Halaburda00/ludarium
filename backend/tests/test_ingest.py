from collections.abc import Iterator
from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, make_account
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api import ingest as ingest_api
from ludarium.enums import (
    EntitlementOrigin,
    EntityType,
    ProviderKind,
    SourceKind,
    SyncTrigger,
)
from ludarium.models import Account, Entitlement, FieldProvenance, Provider, SyncRun

ACCOUNT = {"provider": "steam", "external_account_id": "galaxy-steam-1", "label": "From Galaxy"}


def item(item_id: str, title: str, **changes: Any) -> dict[str, Any]:
    return {"provider_item_id": item_id, "title": title} | changes


GAMES = [
    item("292030", "The Witcher 3: Wild Hunt", playtime_minutes=6000),
    item("378648", "The Witcher 3: Hearts of Stone", item_kind="dlc", parent_item_id="292030"),
    item("620", "Portal 2", last_played_at="2026-09-01T20:00:00+02:00"),
]


def payload(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "reporter": "manual",
        "account": ACCOUNT,
        "complete": True,
        "items": GAMES,
    } | changes


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    response = client.post(
        "/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    return client


def send(client: TestClient, body: dict[str, Any], expected: int = 200) -> dict[str, Any]:
    response = client.post("/api/ingest", json=body)
    assert response.status_code == expected, response.text
    answer: dict[str, Any] = response.json()
    return answer


def titles(client: TestClient, query: str = "") -> set[str]:
    works = client.get(f"/api/works?{query}").json()["works"]
    return {work["title"] for work in works}


async def count(session: AsyncSession, model: type[Any]) -> int:
    total = await session.scalar(select(func.count()).select_from(model))
    await session.commit()
    return total or 0


async def test_a_report_lands_in_a_derived_account_as_a_sync_would(
    signed_in: TestClient, session: AsyncSession
) -> None:
    run = send(signed_in, payload())

    assert (run["provider"], run["trigger"], run["status"]) == ("manual", "ingest", "success")
    assert (run["items_seen"], run["items_added"]) == (3, 3)
    # The add-on folds under its game, as a synced one does.
    assert titles(signed_in) == {"The Witcher 3: Wild Hunt", "Portal 2"}

    [account] = (await session.scalars(select(Account))).all()
    assert (account.is_derived, account.credentials_encrypted, account.label) == (
        True,
        None,
        "From Galaxy",
    )
    origins = set(await session.scalars(select(Entitlement.origin)))
    assert origins == {EntitlementOrigin.IMPORT}
    portal = await session.scalar(select(Entitlement).where(Entitlement.provider_item_id == "620"))
    assert portal is not None and portal.last_played_at is not None
    assert portal.last_played_at.isoformat() == "2026-09-01T18:00:00+00:00"
    await session.commit()


async def test_the_reporter_is_the_source_and_the_run_s_provider(
    signed_in: TestClient, session: AsyncSession
) -> None:
    # The Galaxy upload's shape (docs/schema.md): an agent reports for a
    # platform's account, and it is the agent's rung that lands on provenance.
    session.add(
        Provider(
            key="galaxy",
            kind=ProviderKind.AGENT,
            source_kind=SourceKind.LOCAL_AGENT,
            display_name="GOG Galaxy",
        )
    )
    await session.commit()

    send(signed_in, payload(reporter="galaxy"))

    kinds = set(
        await session.execute(
            select(FieldProvenance.source_kind, FieldProvenance.source_ref).where(
                FieldProvenance.entity_type == EntityType.ENTITLEMENT
            )
        )
    )
    assert kinds == {(SourceKind.LOCAL_AGENT, "galaxy")}
    run = await session.scalar(select(SyncRun))
    galaxy = await session.scalar(select(Provider).where(Provider.key == "galaxy"))
    account = await session.scalar(select(Account))
    assert run is not None and galaxy is not None and account is not None
    assert (run.provider_id, run.account_id, run.trigger) == (
        galaxy.id,
        account.id,
        SyncTrigger.INGEST,
    )
    await session.commit()


async def test_a_second_report_updates_rather_than_duplicates(
    signed_in: TestClient, session: AsyncSession
) -> None:
    send(signed_in, payload())
    run = send(
        signed_in, payload(account=ACCOUNT | {"label": "Renamed"}, items=GAMES[:2], complete=False)
    )

    assert (run["items_added"], run["items_updated"], run["items_removed"]) == (0, 2, 0)
    assert await count(session, Account) == 1
    assert await count(session, Entitlement) == 3
    # The label is the account's from its first report on.
    assert await session.scalar(select(Account.label)) == "From Galaxy"
    await session.commit()


def test_only_a_complete_report_removes_what_it_leaves_out(signed_in: TestClient) -> None:
    send(signed_in, payload())

    # A report of what changed says nothing about the rest (rule 1).
    send(signed_in, payload(items=GAMES[:1], complete=False))
    assert titles(signed_in) == {"The Witcher 3: Wild Hunt", "Portal 2"}
    # Left out, `complete` is false: the safe reading.
    body = payload(items=GAMES[:1])
    del body["complete"]
    assert send(signed_in, body)["items_removed"] == 0

    run = send(signed_in, payload(items=GAMES[:1], complete=True))
    assert run["items_removed"] == 2
    removed = signed_in.get("/api/entitlements/removed").json()
    assert {copy["provider_title"] for copy in removed} == {
        "The Witcher 3: Hearts of Stone",
        "Portal 2",
    }


async def test_a_connected_account_is_reported_by_its_own_sync(
    signed_in: TestClient, session: AsyncSession
) -> None:
    await make_account(session, external_account_id="galaxy-steam-1")
    await session.commit()

    answer = send(signed_in, payload(), expected=409)

    assert "connected" in answer["detail"]
    assert await count(session, Entitlement) == 0
    assert await count(session, SyncRun) == 0


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"reporter": "nobody"}, "`nobody` cannot report"),
        # A platform with a client of ours reports through its own sync.
        ({"reporter": "steam"}, "`steam` cannot report"),
        ({"reporter": "igdb"}, "`igdb` cannot report"),
        ({"account": ACCOUNT | {"provider": "igdb"}}, "`igdb` is not a platform"),
        # The manual entries' account is not one a report can address.
        ({"account": ACCOUNT | {"provider": "manual"}}, "`manual` is not a platform"),
        ({"account": ACCOUNT | {"provider": "nowhere"}}, "`nowhere` is not a platform"),
    ],
)
async def test_a_report_from_or_about_the_wrong_provider_is_refused(
    signed_in: TestClient, session: AsyncSession, changes: dict[str, Any], detail: str
) -> None:
    answer = send(signed_in, payload(**changes), expected=422)

    assert detail in answer["detail"]
    assert await count(session, Account) == 0


@pytest.mark.parametrize(
    ("changes", "where", "message"),
    [
        ({"version": 2}, ["body", "version"], "version 2 is not one this server reads; it reads 1"),
        (
            {"items": [item("1", "Mine", installed=True)]},
            ["body", "items", 0, "installed"],
            "local agent",
        ),
        (
            {"items": [item("1", "Mine", last_played_at="2026-09-01T20:00:00")]},
            ["body", "items", 0, "last_played_at"],
            "timezone",
        ),
        ({"account": {"provider": "steam", "label": "No id"}}, ["body", "account"], "Field"),
        ({"surprise": True}, ["body", "surprise"], "Extra"),
    ],
)
def test_a_malformed_report_is_refused_whole(
    signed_in: TestClient, changes: dict[str, Any], where: list[Any], message: str
) -> None:
    answer = send(signed_in, payload(**changes), expected=422)

    errors = answer["detail"]
    assert any(
        error["loc"][: len(where)] == where and message in error["msg"] for error in errors
    ), errors
    assert titles(signed_in) == set()


def test_an_oversized_report_is_refused_before_it_is_read(
    signed_in: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ingest_api, "MAX_BYTES", 1000)

    declared = signed_in.post("/api/ingest", json=payload())
    assert declared.status_code == 200

    big = payload(items=[item(str(n), f"Game {n}") for n in range(100)])
    assert signed_in.post("/api/ingest", json=big).status_code == 413

    # Sent without a length, it is counted as it arrives.
    def chunks() -> Iterator[bytes]:
        # Never valid JSON: refused for its size before anything parses it.
        yield b"x" * 600
        yield b"x" * 600

    response = signed_in.post(
        "/api/ingest", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert "at most" in response.json()["detail"]


def test_ingest_needs_a_session(client: TestClient) -> None:
    assert client.post("/api/ingest", json=payload()).status_code == 401


async def test_a_switched_off_account_takes_no_report(
    signed_in: TestClient, session: AsyncSession
) -> None:
    send(signed_in, payload())
    account = await session.scalar(select(Account))
    assert account is not None
    await session.commit()
    assert (
        signed_in.patch(f"/api/accounts/{account.id}", json={"is_active": False}).status_code == 200
    )

    answer = send(signed_in, payload(items=GAMES[:1]), expected=409)

    assert "switched off" in answer["detail"]
    # Nothing was swept: switching off is not removing (rule 1).
    assert signed_in.get("/api/entitlements/removed").json() == []


def test_a_reported_copy_links_to_no_store(signed_in: TestClient) -> None:
    send(signed_in, payload())

    works = signed_in.get("/api/works").json()["works"]
    copies = [copy for work in works for copy in work["entitlements"]]

    # `292030` reads as an appid, but only Steam's own answer makes it one (ADR-0039).
    assert copies
    assert {copy["store_url"] for copy in copies} == {None}
