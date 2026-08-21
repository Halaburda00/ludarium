"""The committed contract, and what keeps it committed truthfully.

`docs/openapi.json` exists because the frontend generates its types from it and
CI's frontend job has no Python in it (#35). A copy is only worth having if
nothing can change the API without changing the copy, which is what these two
assertions are between them: the file is what the command prints, and the
command prints what the app serves.
"""

import pytest
from conftest import BACKEND_ROOT
from fastapi.testclient import TestClient

from ludarium.openapi import document, main

COMMITTED = BACKEND_ROOT.parent / "docs" / "openapi.json"

REGENERATE = (
    "`docs/openapi.json` is behind the API — regenerate it, and the types built "
    "from it:\n"
    "  cd backend && uv run ludarium-openapi > ../docs/openapi.json\n"
    "  cd frontend && pnpm run api:types"
)


def test_the_committed_document_is_what_the_command_prints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Byte for byte, not shape for shape.

    Comparing the parsed JSON would pass on a file whose formatting the command
    no longer produces, and the next regeneration would then be a diff nobody
    asked for on top of the one they did.
    """

    main()
    assert capsys.readouterr().out == COMMITTED.read_text(encoding="utf-8"), REGENERATE


def test_the_printed_document_is_the_one_the_app_serves(client: TestClient) -> None:
    """The command builds its own app, from an environment it invents.

    Nothing in the document depends on a setting, and this is what says so — if
    it ever stops being true, the committed copy would describe an instance
    nobody runs.
    """

    assert document() == client.get("/openapi.json").json()
