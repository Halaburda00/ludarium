"""What every library provider looks like from the outside.

Two operations, because that is what M1 needs and what M4 plugs GOG and Epic
into. The value of defining it now is the shape of the errors: rule 1 lets only
a `success` run mark entitlements removed, so "Steam said no" and "Steam did not
answer" have to be different types before the sync service exists to tell them
apart.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ludarium.enums import ItemKind, OwnershipType


class ProviderError(Exception):
    """Anything a provider can fail with. Never carries a credential (rule 7)."""


class InvalidCredentialsError(ProviderError):
    """The platform rejected the key. Onboarding can say so immediately."""


class LibraryNotVisibleError(ProviderError):
    """The key works, the library is private.

    Its own type because the fix is the user's privacy settings, not a new key —
    told otherwise they would rotate credentials that were never the problem.
    """


class RateLimitedError(ProviderError):
    """Asked to slow down. `retry_after` is the platform's own figure, if it gave one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailableError(ProviderError):
    """No usable answer: a transport failure, or a server error that outlived the retries.

    Distinct from every error above because a run that ends here removes
    nothing (rule 1). An Epic outage emptying the Epic library is exactly the
    failure this type exists to prevent.
    """


class MalformedResponseError(ProviderError):
    """A 200 whose body is not what the API documents. Never retried: it would not help."""


@dataclass(frozen=True, slots=True)
class LibraryItem:
    """One owned item, normalised. The same shape the M4 ingest contract carries.

    Deliberately not the platform's dict: the sync service should not learn what
    `rtime_last_played` means, and a second provider would bring a second
    spelling of the same idea.

    `installed` is absent on purpose — a library API cannot know it, and the
    local agent reports it through ingest (rule 5's field-level exception).
    """

    provider_item_id: str
    title: str
    ownership_type: OwnershipType = OwnershipType.OWNED
    # None where the platform does not say; the resolver keeps its own default.
    item_kind: ItemKind | None = None
    playtime_minutes: int | None = None
    last_played_at: datetime | None = None
    acquired_at: datetime | None = None
    # As received, for debugging a bad match. Credentials travel in the request,
    # never in the response, so nothing has to be stripped here (rule 7).
    raw: dict[str, Any] = field(default_factory=dict)


class LibraryProvider(Protocol):
    """A connected account, ready to be asked what it owns."""

    key: str

    async def validate_credentials(self) -> None:
        """Return if the credentials work, raise the reason if they do not.

        No boolean: onboarding has to tell "wrong key" from "private profile"
        from "Steam is down", and only one of the three is the user's to fix.
        """
        ...

    async def fetch_library(self) -> list[LibraryItem]:
        """Everything the account owns. Partial results are a failure, not a library."""
        ...
