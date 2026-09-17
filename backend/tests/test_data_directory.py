"""The cache convention as a mechanism: fetched data cannot reach git or an image (ADR-0019).

IGDB and RAWG responses live in the database, and the database lives in the data
directory. These tests hold both ignore files to that, against the path the
settings actually default to rather than one written down beside them — so
moving the default database somewhere unignored fails here, not in a published
image.
"""

import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import make_url

from ludarium.config import Settings

REPOSITORY = Path(__file__).resolve().parents[2]
BACKEND = REPOSITORY / "backend"


def default_database() -> str:
    database = make_url(Settings.model_fields["database_url"].default).database
    assert database is not None
    # Relative to where the backend is run from, which is `backend/`.
    return (BACKEND / database).resolve().relative_to(REPOSITORY).as_posix()


RUNTIME_STATE = [
    default_database(),
    f"{default_database()}-wal",
    # Where #51 puts cover files: beside the database, in the same directory.
    f"{Path(default_database()).parent.as_posix()}/covers/igdb/co1wyy.jpg",
    "backend/.env",
]
SOURCE = ["backend/src/ludarium/enrichment.py", "backend/.env.example"]


def git_ignores(path: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY), "check-ignore", "--quiet", "--no-index", path],
        check=False,
    )
    return result.returncode == 0


def docker_pattern(pattern: str) -> re.Pattern[str]:
    """One `.dockerignore` line as Docker reads it: anchored at the context root.

    `**` spans directories, `*` and `?` do not, and a trailing slash is dropped
    by the path cleaning Docker applies before matching.
    """

    expression = ""
    rest = pattern.strip("/")
    while rest:
        if rest.startswith("**/"):
            expression, rest = expression + "(?:.*/)?", rest[3:]
        elif rest.startswith("**"):
            expression, rest = expression + ".*", rest[2:]
        elif rest[0] == "*":
            expression, rest = expression + "[^/]*", rest[1:]
        elif rest[0] == "?":
            expression, rest = expression + "[^/]", rest[1:]
        else:
            expression, rest = expression + re.escape(rest[0]), rest[1:]
    return re.compile(expression)


def docker_ignores(path: str) -> bool:
    """Excluded if the path or any directory above it matches; the last matching line wins."""

    lines = (REPOSITORY / ".dockerignore").read_text().splitlines()
    parts = path.split("/")
    candidates = ["/".join(parts[: depth + 1]) for depth in range(len(parts))]
    excluded = False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = docker_pattern(line.removeprefix("!"))
        if any(pattern.fullmatch(candidate) for candidate in candidates):
            excluded = not negated
    return excluded


def test_the_default_database_is_in_a_data_directory() -> None:
    assert Path(default_database()).parent.name == "data"


@pytest.mark.parametrize("path", RUNTIME_STATE)
def test_git_never_sees_runtime_state(path: str) -> None:
    assert git_ignores(path)


@pytest.mark.parametrize("path", RUNTIME_STATE)
def test_an_image_never_carries_runtime_state(path: str) -> None:
    assert docker_ignores(path)


@pytest.mark.parametrize("path", SOURCE)
def test_the_ignore_files_leave_the_source_alone(path: str) -> None:
    """Without this, `*` in either file would pass every test above."""

    assert not git_ignores(path)
    assert not docker_ignores(path)
