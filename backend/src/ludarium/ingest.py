"""The one shape every source without an API of ours reports a library in (rule 8, ADR-0035).

A report names who is reporting, the account it describes and what that account
owns, and it is applied by `sync_account` like any platform's answer: the same
provenance, stubs, add-on folding, aggregates and run history. The CSV import,
the Galaxy upload and the local agent differ only in how they build a report.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import EntitlementOrigin, ItemKind, OwnershipType, ProviderKind, SyncTrigger
from ludarium.filters import MAX_MINUTES
from ludarium.models import Account, Provider, SyncRun
from ludarium.providers.base import FetchedLibrary, LibraryItem
from ludarium.sync import sync_account

# The versions of the contract this server reads. A version moves only for a
# change an existing client would break on (ADR-0035).
VERSIONS: Final = frozenset({1})
# Several times the largest library measured (a Galaxy database with every
# platform connected), and a bound on what one request can make us hold.
MAX_ITEMS: Final = 50_000
# Who may report: sources with no client of ours. A platform with one reports
# through its own sync, and a metadata provider describes works, not accounts.
REPORTERS: Final = frozenset({ProviderKind.AGENT, ProviderKind.MANUAL})

type Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class IngestAccount(BaseModel):
    """The account a report describes: on which platform, and which one there."""

    model_config = ConfigDict(extra="forbid")

    provider: Key
    # Required: it is half the account's identity, and a report that cannot
    # name its account would land in a new one every time.
    external_account_id: Key
    # Used when the account is first seen. Later reports leave it alone, so a
    # name the user gave it is not undone by the next upload.
    label: Key


class IngestItem(BaseModel):
    """One owned item, as `LibraryItem` carries it."""

    model_config = ConfigDict(extra="forbid")

    provider_item_id: Key
    # Not required to be non-blank: a platform can have nameless entries, and
    # the stub names them by their id (`sync._nameless`).
    title: str = Field(max_length=1024)
    ownership_type: OwnershipType = OwnershipType.OWNED
    item_kind: ItemKind | None = None
    playtime_minutes: int | None = Field(default=None, ge=0, le=MAX_MINUTES)
    last_played_at: AwareDatetime | None = None
    acquired_at: AwareDatetime | None = None
    # The `provider_item_id` of the game an add-on belongs to, in this report.
    parent_item_id: Key | None = None
    # Part of the contract, and refused until something may send it: whether a
    # game is installed is known only to the local agent (rule 5).
    installed: bool | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("installed")
    @classmethod
    def _not_yet(cls, value: bool | None) -> bool | None:
        if value is not None:
            raise ValueError("is reported by the local agent, which does not exist yet")
        return value

    def item(self) -> LibraryItem:
        return LibraryItem(
            provider_item_id=self.provider_item_id,
            title=self.title,
            ownership_type=self.ownership_type,
            item_kind=self.item_kind,
            playtime_minutes=self.playtime_minutes,
            last_played_at=_utc(self.last_played_at),
            acquired_at=_utc(self.acquired_at),
            parent_item_id=self.parent_item_id,
            raw=self.raw,
        )


class IngestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    reporter: Key
    account: IngestAccount
    # Whether `items` is everything the account owns. Only a whole library may
    # mark what it leaves out as removed (rule 1), so a report has to say so;
    # leaving it out is the safe reading.
    complete: bool = False
    items: list[IngestItem] = Field(max_length=MAX_ITEMS)

    @field_validator("version")
    @classmethod
    def _known(cls, value: int) -> int:
        if value not in VERSIONS:
            taken = ", ".join(str(version) for version in sorted(VERSIONS))
            raise ValueError(f"version {value} is not one this server reads; it reads {taken}")
        return value


class IngestRefusedError(Exception):
    """The report names a reporter or a platform that cannot be used this way."""


class ConnectedAccountError(IngestRefusedError):
    """The report describes an account the user connected, whose own sync reports it."""


class ReportedLibrary:
    """A report, answering as a platform would."""

    renewed_secret: str | None = None

    def __init__(self, key: str, report: IngestReport) -> None:
        self.key = key
        self._report = report

    async def validate_credentials(self) -> None:
        return None

    async def fetch_library(self) -> FetchedLibrary:
        return FetchedLibrary(
            items=[item.item() for item in self._report.items],
            complete=self._report.complete,
        )


async def ingest(
    session: AsyncSession,
    report: IngestReport,
    *,
    user_id: int,
    trigger: SyncTrigger = SyncTrigger.INGEST,
) -> SyncRun:
    """Apply a report to its account, made derived if new, and return the finished run.

    Refused rather than applied to an account the user connected: that account
    is synced from its platform, and a second writer reporting at another rung
    of rule 5 would hold a second opinion about every game in it.
    """

    reporter = await _provider(session, report.reporter)
    if reporter is None or reporter.kind not in REPORTERS:
        raise IngestRefusedError(f"`{report.reporter}` cannot report a library through ingest")
    platform = await _provider(session, report.account.provider)
    if platform is None or platform.kind is not ProviderKind.PLATFORM:
        raise IngestRefusedError(f"`{report.account.provider}` is not a platform")

    account = await session.scalar(
        select(Account).where(
            Account.provider_id == platform.id,
            Account.external_account_id == report.account.external_account_id,
        )
    )
    if account is not None and (not account.is_derived or account.user_id != user_id):
        raise ConnectedAccountError(
            f"this `{platform.key}` account is connected, and its own sync reports it"
        )
    if account is None:
        account = Account(
            user_id=user_id,
            provider_id=platform.id,
            external_account_id=report.account.external_account_id,
            label=report.account.label,
            is_derived=True,
        )
        session.add(account)
        await session.commit()

    return await sync_account(
        session,
        account=account,
        library=ReportedLibrary(reporter.key, report),
        trigger=trigger,
        origin=EntitlementOrigin.IMPORT,
    )


async def _provider(session: AsyncSession, key: str) -> Provider | None:
    provider: Provider | None = await session.scalar(select(Provider).where(Provider.key == key))
    return provider


def _utc(moment: datetime | None) -> datetime | None:
    # Aware by validation; normalised so a `+02:00` stamp compares with the rest.
    return None if moment is None else moment.astimezone(UTC)
