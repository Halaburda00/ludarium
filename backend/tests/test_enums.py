from enum import StrEnum

import pytest

from ludarium import enums

# Copied from docs/schema.md. The point is to fail when the code drifts from the
# document, so this list is written out rather than derived from the enums.
EXPECTED: dict[type[StrEnum], list[str]] = {
    enums.OwnershipType: ["owned", "subscription", "free", "family_shared", "trial", "physical"],
    enums.ItemKind: ["game", "dlc", "demo", "soundtrack", "video", "tool", "mod"],
    enums.PlayStatus: [
        "not_started",
        "playing",
        "completed",
        "mastered",
        "dropped",
        "on_hold",
        "wishlist",
    ],
    enums.SourceKind: ["manual", "platform_api", "local_agent", "metadata_provider"],
    enums.ProviderKind: ["platform", "metadata", "agent", "manual"],
    enums.LicenceClass: ["redistributable", "runtime_only"],
    enums.EntitlementOrigin: ["sync", "manual", "import", "agent"],
    enums.WorkLinkRole: ["primary", "granted"],
    enums.EntityType: ["work", "edition", "entitlement", "account"],
    enums.FieldStrategy: [
        "precedence",
        "max",
        "sum",
        "latest",
        "agent_only",
        "single_source",
        "user_only",
        "derived",
    ],
    enums.SyncStatus: ["pending", "running", "success", "partial", "failed"],
    enums.SyncTrigger: ["manual", "scheduled", "ingest", "import"],
    enums.MatchLayer: ["hard_id", "alias", "fuzzy", "llm", "manual"],
    enums.MatchStatus: ["pending", "accepted", "rejected", "superseded"],
    enums.ImageKind: ["cover", "hero", "logo", "screenshot"],
    enums.CompanyRole: ["developer", "publisher", "porting", "support"],
}


@pytest.mark.parametrize("enum_class", EXPECTED)
def test_values_match_the_schema_document(enum_class: type[StrEnum]) -> None:
    assert [member.value for member in enum_class] == EXPECTED[enum_class]


def test_source_kind_is_ordered_by_precedence() -> None:
    # Rule 5 reads the ladder off this order, highest first.
    assert tuple(enums.SourceKind) == (
        enums.SourceKind.MANUAL,
        enums.SourceKind.PLATFORM_API,
        enums.SourceKind.LOCAL_AGENT,
        enums.SourceKind.METADATA_PROVIDER,
    )
