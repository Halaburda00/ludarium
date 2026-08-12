"""Every enum in `docs/schema.md`, including values nothing writes yet.

They are `TEXT` + `CHECK` in the database, never a native PostgreSQL type:
adding a value must not require a type migration, and the values stay readable
in a raw SQLite session.
"""

from enum import StrEnum


class OwnershipType(StrEnum):
    OWNED = "owned"
    SUBSCRIPTION = "subscription"
    FREE = "free"
    FAMILY_SHARED = "family_shared"
    TRIAL = "trial"
    PHYSICAL = "physical"


class ItemKind(StrEnum):
    GAME = "game"
    DLC = "dlc"
    DEMO = "demo"
    SOUNDTRACK = "soundtrack"
    VIDEO = "video"
    TOOL = "tool"
    MOD = "mod"


class PlayStatus(StrEnum):
    NOT_STARTED = "not_started"
    PLAYING = "playing"
    COMPLETED = "completed"
    MASTERED = "mastered"
    DROPPED = "dropped"
    ON_HOLD = "on_hold"
    WISHLIST = "wishlist"


class SourceKind(StrEnum):
    """The precedence ladder of architecture rule 5, highest first."""

    MANUAL = "manual"
    PLATFORM_API = "platform_api"
    LOCAL_AGENT = "local_agent"
    METADATA_PROVIDER = "metadata_provider"


class ProviderKind(StrEnum):
    PLATFORM = "platform"
    METADATA = "metadata"
    AGENT = "agent"
    MANUAL = "manual"


class LicenceClass(StrEnum):
    """Whether data from a provider may leave the instance."""

    REDISTRIBUTABLE = "redistributable"
    RUNTIME_ONLY = "runtime_only"


class EntitlementOrigin(StrEnum):
    SYNC = "sync"
    MANUAL = "manual"
    IMPORT = "import"
    AGENT = "agent"


class WorkLinkRole(StrEnum):
    PRIMARY = "primary"
    GRANTED = "granted"


class EntityType(StrEnum):
    """Discriminator for the polymorphic tables."""

    WORK = "work"
    EDITION = "edition"
    ENTITLEMENT = "entitlement"
    ACCOUNT = "account"


class FieldStrategy(StrEnum):
    PRECEDENCE = "precedence"
    MAX = "max"
    SUM = "sum"
    LATEST = "latest"
    AGENT_ONLY = "agent_only"
    SINGLE_SOURCE = "single_source"
    USER_ONLY = "user_only"
    DERIVED = "derived"


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class SyncTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    INGEST = "ingest"
    IMPORT = "import"


class MatchLayer(StrEnum):
    HARD_ID = "hard_id"
    ALIAS = "alias"
    FUZZY = "fuzzy"
    LLM = "llm"
    MANUAL = "manual"


class MatchStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ImageKind(StrEnum):
    COVER = "cover"
    HERO = "hero"
    LOGO = "logo"
    SCREENSHOT = "screenshot"


class CompanyRole(StrEnum):
    DEVELOPER = "developer"
    PUBLISHER = "publisher"
    PORTING = "porting"
    SUPPORT = "support"
