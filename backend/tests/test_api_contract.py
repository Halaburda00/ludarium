"""What the published contract may and may not contain, across every endpoint at once.

Written against `/openapi.json` rather than against each response model, because
the claim is about all of them — including the ones #11 and M2 have not added
yet.
"""

from typing import Any

from fastapi.testclient import TestClient

# Names that must never reach a client. `credentials_encrypted` is on the list
# for the same reason as the plaintext: ciphertext in a response is the secret,
# one key away.
FORBIDDEN = {
    "password",
    "password_hash",
    "credentials_encrypted",
    "api_key",
    "secret",
    "token",
    "token_hash",
}

# Response schemas we know exist, so a walk that quietly finds nothing cannot
# pass by finding nothing.
EXPECTED = {"AccountResponse", "SessionResponse", "SyncRunResponse"}


def referenced(node: Any) -> set[str]:
    """Every `components.schemas` name reachable from one fragment of the document."""

    if isinstance(node, dict):
        found: set[str] = set()
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            found.add(reference.rsplit("/", 1)[1])
        for value in node.values():
            found |= referenced(value)
        return found
    if isinstance(node, list):
        return {name for item in node for name in referenced(item)}
    return set()


def response_schemas(contract: dict[str, Any]) -> set[str]:
    """Transitively, so a credential nested two models deep is still caught."""

    schemas = contract["components"]["schemas"]
    reachable: set[str] = set()
    pending = {
        name
        for operations in contract["paths"].values()
        for operation in operations.values()
        for name in referenced(operation.get("responses", {}))
    }
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending |= referenced(schemas[name])
    return reachable


def test_no_response_schema_exposes_a_credential(client: TestClient) -> None:
    """DoD for #10, and a standing guard for every endpoint added after it.

    Request schemas are deliberately outside this: `LoginRequest.password` and
    `ConnectRequest.credentials` are how a credential arrives, which is the one
    direction it is allowed to travel.
    """

    contract = client.get("/openapi.json").json()
    reachable = response_schemas(contract)
    assert reachable >= EXPECTED, reachable

    exposed = {
        f"{name}.{field}"
        for name in reachable
        for field in contract["components"]["schemas"][name].get("properties", {})
        if field in FORBIDDEN
    }

    assert exposed == set()
