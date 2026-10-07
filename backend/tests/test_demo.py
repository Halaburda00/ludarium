from collections.abc import Iterator
from typing import Any

import pytest
from conftest import TEST_PASSWORD, TEST_USERNAME, create_schema, sync_url
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from ludarium.config import Settings
from ludarium.demo import DECISIONS, EPIC, GONE, MANUAL, STEAM, VIEWS, DemoRefusedError
from ludarium.enums import PlayStatus
from ludarium.main import create_app
from ludarium.models import Account, Provider, SavedView, Work

MANY = 200


@pytest.fixture
def demo(settings: Settings) -> Settings:
    create_schema(settings.database_url)
    return settings.model_copy(update={"demo": True})


@pytest.fixture
def visitor(demo: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(demo)) as client:
        yield client


def listing(client: TestClient, query: str = "") -> list[dict[str, Any]]:
    response = client.get(f"/api/works?limit={MANY}&{query}")
    assert response.status_code == 200
    works: list[dict[str, Any]] = response.json()["works"]
    return works


def titles(works: list[dict[str, Any]]) -> set[str]:
    return {work["title"] for work in works}


def test_the_dataset_names_every_work_once() -> None:
    named = [game.title for game in (*STEAM, GONE, *EPIC)] + [entry.title for entry in MANUAL]

    assert len(named) == len(set(named))
    # A decision about a work the dataset does not have would fail at start.
    assert {decision.title for decision in DECISIONS} <= set(named)


def test_a_visitor_sees_the_library_without_signing_in(visitor: TestClient) -> None:
    shown = titles(listing(visitor))

    # Add-ons fold under their game, the hidden giveaway is left out, and the
    # game the platform stopped listing is in the removed view instead.
    assert "Lanterns of Vael" in shown
    assert "Lanterns of Vael: The Ember Coast" not in shown
    assert "Starfall Arcade" not in shown
    assert GONE.title not in shown
    assert {entry.title for entry in MANUAL} <= shown
    removed = visitor.get("/api/entitlements/removed").json()
    assert [copy["provider_title"] for copy in removed] == [GONE.title]


def test_every_m3_feature_has_something_to_show(visitor: TestClient) -> None:
    queue = listing(visitor, "status=queued&sort=queue")
    assert [work["title"] for work in queue] == [
        decision.title for decision in DECISIONS if decision.state.play_status is PlayStatus.QUEUED
    ]
    assert [work["queue_position"] for work in queue] == list(range(1, len(queue) + 1))

    assert listing(visitor, "hidden=only")[0]["title"] == "Starfall Arcade"
    assert titles(listing(visitor, "genre=tactics")) == {"Gloamwood Tactics"}
    assert titles(listing(visitor, "year_min=2024&kind=game")) == {
        "Ashen Cartography",
        "Velvet Circuit",
    }
    assert titles(listing(visitor, "kind=soundtrack&addons=separate")) == {
        "Mirelight Original Soundtrack"
    }
    played = listing(visitor, "sort=playtime&order=desc")
    assert played[0]["title"] == "Lanterns of Vael"
    assert played[0]["addon_count"] == 1

    views = visitor.get("/api/views").json()
    assert [view["name"] for view in views] == [name for name, _ in VIEWS]
    assert all(view["dropped"] == [] for view in views)

    lanterns = next(work for work in played if work["title"] == "Lanterns of Vael")
    detail = visitor.get(f"/api/works/{lanterns['id']}").json()
    assert detail["release_date"] == "2019-03-14"
    assert [genre["slug"] for genre in detail["genres"]] == ["adventure", "role-playing"]
    assert detail["rating"] == 9
    assert detail["completed_at"] is not None


def test_the_demo_links_to_no_store_and_shows_no_score(visitor: TestClient) -> None:
    works = listing(visitor, "hidden=include&addons=separate")

    assert all(copy["store_url"] is None for work in works for copy in work["entitlements"])
    assert all(work["metacritic"] is None and work["steam_reviews"] is None for work in works)
    assert all(work["cover"] is None for work in works)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/auth/login", {"username": TEST_USERNAME, "password": TEST_PASSWORD}),
        ("POST", "/api/auth/logout", None),
        ("PATCH", "/api/works/1/state", {"rating": 1}),
        ("PUT", "/api/works/1/queue", {"position": 1}),
        ("POST", "/api/sync/steam", None),
        ("POST", "/api/views", {"name": "Mine", "query": ""}),
        ("DELETE", "/api/views/1", None),
        ("POST", "/api/entitlements/manual", {"title": "Defaced"}),
    ],
)
def test_every_write_is_refused(
    visitor: TestClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    before = listing(visitor, "hidden=include&addons=separate")

    response = visitor.request(method, path, json=body)

    assert response.status_code == 403
    assert response.json() == {"detail": "the demo is read-only"}
    assert listing(visitor, "hidden=include&addons=separate") == before


def test_the_health_check_says_it_is_a_demo(visitor: TestClient) -> None:
    assert visitor.get("/api/health").json()["demo"] is True


def test_a_restart_keeps_the_demo_it_made(demo: Settings) -> None:
    with TestClient(create_app(demo)) as client:
        first = listing(client, "hidden=include&addons=separate")
    with TestClient(create_app(demo)) as client:
        assert listing(client, "hidden=include&addons=separate") == first


def test_a_database_with_a_library_of_its_own_is_refused(demo: Settings) -> None:
    with TestClient(create_app(demo.model_copy(update={"demo": False}))):
        pass
    engine = create_engine(sync_url(demo.database_url))
    with Session(engine) as session:
        steam = session.scalars(select(Provider).where(Provider.key == "steam")).one()
        session.add(Account(provider_id=steam.id, external_account_id="76561197", label="Mine"))
        session.commit()
    engine.dispose()

    with (
        pytest.raises(DemoRefusedError, match="library of its own"),
        TestClient(create_app(demo)),
    ):
        pass  # pragma: no cover


def test_a_demo_left_half_made_is_refused(demo: Settings) -> None:
    with TestClient(create_app(demo)):
        pass
    engine = create_engine(sync_url(demo.database_url))
    with engine.begin() as connection:
        connection.execute(delete(SavedView))
    engine.dispose()

    with pytest.raises(DemoRefusedError, match="half-made"), TestClient(create_app(demo)):
        pass  # pragma: no cover


def test_the_same_database_without_the_flag_asks_for_a_session(demo: Settings) -> None:
    with TestClient(create_app(demo)):
        pass

    with TestClient(create_app(demo.model_copy(update={"demo": False}))) as client:
        assert client.get("/api/works").status_code == 401
        assert client.get("/api/health").json()["demo"] is False
        # And the store links come back with the flag.
        engine = create_engine(sync_url(demo.database_url))
        with engine.connect() as connection:
            template = connection.execute(
                text("SELECT store_url_template FROM provider WHERE key = 'steam'")
            ).scalar_one()
        engine.dispose()
        assert template is not None


def test_the_demo_makes_no_work_without_a_copy(visitor: TestClient, demo: Settings) -> None:
    engine = create_engine(sync_url(demo.database_url))
    with Session(engine) as session:
        made = session.scalars(select(Work.title)).all()
    engine.dispose()

    assert len(made) == len(STEAM) + 1 + len(EPIC) + len(MANUAL)
