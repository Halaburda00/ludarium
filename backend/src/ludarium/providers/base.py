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

import httpx

from ludarium.enums import ItemKind, OwnershipType, SyncErrorKind


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


def error_kind(exc: BaseException) -> SyncErrorKind:
    """Who can fix a failed run, read off the error that ended it.

    Stored with the run so a client can say "sign in again" for an ended
    sign-in and "try later" for an outage without parsing a message.
    """

    for kind, types in (
        (SyncErrorKind.CREDENTIALS, InvalidCredentialsError),
        (SyncErrorKind.NOT_VISIBLE, LibraryNotVisibleError),
        (SyncErrorKind.RATE_LIMITED, RateLimitedError),
        (SyncErrorKind.UNAVAILABLE, ProviderUnavailableError),
        (SyncErrorKind.MALFORMED, MalformedResponseError),
    ):
        if isinstance(exc, types):
            return kind
    return SyncErrorKind.OTHER


def whole_number(value: object) -> int | None:
    """An integer the platform actually sent.

    Shared rather than written in each provider: `bool` is an `int` in Python and
    is never a number here, and a rule spelled out once per provider is a rule
    that holds in some of them.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The platform's own `Retry-After`, where it gave one in seconds.

    A date-form header is not worth parsing: nothing here schedules a retry that
    far out.
    """

    header = response.headers.get("retry-after", "")
    try:
        return float(header)
    except ValueError:
        return None


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
    # The platform's id for the game an add-on belongs to, on the same account.
    # None where the platform does not say, which for a game is always.
    parent_item_id: str | None = None
    # As received, for debugging a bad match. Credentials travel in the request,
    # never in the response, so nothing has to be stripped here (rule 7).
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FetchedLibrary:
    """What a provider handed over: the items it could read, and how many it could not.

    Two answers rather than one, because an entry the provider cannot name and a
    library that did not arrive are different failures (#44). The first costs
    that entry; the second costs the run.
    """

    items: list[LibraryItem]
    skipped: int = 0


class LibraryProvider(Protocol):
    """A connected account, ready to be asked what it owns."""

    key: str
    # A credential the platform replaced while being asked, to be stored in
    # place of the old one. Epic's refresh token is spent by every use; a key
    # that never changes, like Steam's, leaves this None.
    renewed_secret: str | None

    async def validate_credentials(self) -> None:
        """Return if the credentials work, raise the reason if they do not.

        No boolean: onboarding has to tell "wrong key" from "private profile"
        from "Steam is down", and only one of the three is the user's to fix.
        """
        ...

    async def fetch_library(self) -> FetchedLibrary:
        """Everything the account owns.

        A truncated answer is still a failure, not a library. An entry that
        arrived but cannot be read is counted in `skipped` instead of raised.
        """
        ...
