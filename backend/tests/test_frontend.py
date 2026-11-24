from pathlib import Path

import pytest
from conftest import create_schema
from fastapi.testclient import TestClient

from ludarium.config import ConfigurationError, Settings
from ludarium.frontend import IMMUTABLE, REVALIDATE
from ludarium.main import create_app

PAGE = "<!doctype html><title>Ludarium</title>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(PAGE)
    (root / "assets" / "index-abc123.js").write_text("console.log(1)")
    (root / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside")
    return root


@pytest.fixture
def served(settings: Settings, dist: Path) -> TestClient:
    create_schema(settings.database_url)
    app = create_app(settings.model_copy(update={"frontend_dir": dist}))
    with TestClient(app) as client:
        yield client


def test_a_hashed_asset_is_cached_for_good(served: TestClient) -> None:
    response = served.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.text == "console.log(1)"
    assert response.headers["cache-control"] == IMMUTABLE


def test_a_file_outside_assets_is_revalidated(served: TestClient) -> None:
    response = served.get("/favicon.svg")

    assert (response.status_code, response.headers["cache-control"]) == (200, REVALIDATE)


@pytest.mark.parametrize("path", ["/", "/library", "/works/12?tab=notes", "/assets/gone.js"])
def test_any_other_path_is_the_page_and_needs_no_session(served: TestClient, path: str) -> None:
    response = served.get(path)

    assert response.status_code == 200
    assert response.text == PAGE
    assert response.headers["cache-control"] == REVALIDATE


@pytest.mark.parametrize("path", ["/api", "/api/nothing-here", "/api/works/1/nothing"])
def test_an_unknown_api_path_stays_a_json_404(served: TestClient, path: str) -> None:
    response = served.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"


def test_the_api_keeps_its_own_404_and_405(served: TestClient, client: TestClient) -> None:
    probes = [("POST", "/api/nope"), ("GET", "/api/auth/logout"), ("POST", "/library")]

    with_page = [served.request(method, path).status_code for method, path in probes]
    without = [client.request(method, path).status_code for method, path in probes]

    assert with_page == without == [404, 405, 404]


def test_the_api_still_answers_first(served: TestClient) -> None:
    assert served.get("/api/health").json()["status"] == "ok"
    assert served.get("/api/works").status_code == 401
    assert served.get("/openapi.json").json()["info"]["title"] == "Ludarium"


def test_the_page_is_not_part_of_the_api_contract(served: TestClient, client: TestClient) -> None:
    assert served.get("/openapi.json").json() == client.get("/openapi.json").json()


# httpx collapses a literal `..` before sending; the encoded one reaches us.
@pytest.mark.parametrize("path", ["/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"])
def test_nothing_outside_the_directory_is_served(served: TestClient, path: str) -> None:
    response = served.get(path)

    assert "outside" not in response.text


def test_a_directory_without_a_page_is_refused_at_start(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="pnpm run build"):
        create_app(settings.model_copy(update={"frontend_dir": tmp_path}))


def test_without_the_setting_nothing_but_the_api_is_served(client: TestClient) -> None:
    assert client.get("/library").status_code == 404
