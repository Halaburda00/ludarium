"""The library's filters, declared once each: a query parameter and the SQL it means.

Every filter is a field on `LibraryFilters` whose annotation carries its
`Predicate`. FastAPI reads the same model to validate the query string and to
document it, so a filter cannot exist in the schema without SQL behind it, nor
in SQL without a place in the schema. Adding one is adding a field.

The predicates are written against `_owned_works` in `api.works`: `Work` in the
FROM clause and `UserWorkState` outer-joined for this user. They narrow the
listing and never reorder it, so a filtered page is keyed as the unfiltered
one is, in whichever order was asked for, and pages as the library does.

A null is "not known", never zero or "every value". SQL's comparison already
behaves that way — `NULL >= 80` is not true — and each predicate is written so
it keeps doing so. A work with no Metacritic score is not a work scored below
every threshold.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator
from sqlalchemy import ColumnElement, false, func, select, true

from ludarium.enums import ItemKind, PlayStatus, ProviderKind
from ludarium.models import Account, Entitlement, EntitlementWork, Provider, UserWorkState, Work
from ludarium.queries import owned_by
from ludarium.seed import PROVIDER_SEED

# The widest a list parameter may be. Longer than any real selection — there
# are a dozen provider keys and eight kinds — and short enough that no caller
# builds an `IN (...)` near the bind limit.
MAX_CHOICES = 32
# A century of play, in minutes. Far past any library, and far inside the
# 64-bit integer SQLite binds: past that the driver raises and the answer is a
# 500 rather than a refusal.
MAX_MINUTES = 60 * 24 * 365 * 100

# The filters that come as a `_min` and a `_max` over one value.
RANGES = ("metacritic", "year", "playtime")


# Where a copy can be owned. Taken from the seed rather than the table: the keys
# are code-owned (`seed.py`), and a metadata provider holds no copies, so naming
# one could only ever answer with nothing.
PLATFORMS = frozenset(
    spec.key for spec in PROVIDER_SEED if spec.kind in (ProviderKind.PLATFORM, ProviderKind.MANUAL)
)


def _known_platforms(keys: list[str]) -> list[str]:
    # A typo answered with an empty page reads as "you own nothing there", the
    # same false claim an inverted range would make.
    unknown = sorted(set(keys) - PLATFORMS)
    if unknown:
        known = ", ".join(sorted(PLATFORMS))
        raise ValueError(f"not a platform: {', '.join(unknown)}; expected one of {known}")
    return keys


@dataclass(frozen=True, slots=True)
class Predicate:
    """What a filter's value means in SQL, given the user whose library it narrows."""

    build: Callable[[Any, int], ColumnElement[bool]]


def _on_platform(keys: list[str], user_id: int) -> ColumnElement[bool]:
    # A live copy, by the same `owned_by` the listing itself uses: a work kept
    # by its Steam copy does not match GOG because its GOG copy was removed.
    return (
        select(EntitlementWork.work_id)
        .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
        .join(Account, Account.id == Entitlement.account_id)
        .join(Provider, Provider.id == Account.provider_id)
        .where(EntitlementWork.work_id == Work.id, *owned_by(user_id), Provider.key.in_(keys))
        .exists()
    )


# What the listing shows for a work with no state row (`_describe`), so each
# filter agrees with what is printed on the card.
_playtime = func.coalesce(UserWorkState.playtime_minutes, 0)
_status = func.coalesce(UserWorkState.play_status, PlayStatus.NOT_STARTED.value)
_hidden = func.coalesce(UserWorkState.is_hidden, false())


class Hidden(StrEnum):
    """Whether the works the user hid are in the listing."""

    # The default: hidden means "not in my library view", which is the point of
    # hiding something. The work stays owned and counted; only the view skips it.
    EXCLUDE = "exclude"
    INCLUDE = "include"
    ONLY = "only"


def _on_hidden(hidden: Hidden, _: int) -> ColumnElement[bool]:
    match hidden:
        case Hidden.EXCLUDE:
            return _hidden.is_(false())
        case Hidden.ONLY:
            return _hidden.is_(true())
        case Hidden.INCLUDE:
            return true()


class LibraryFilters(BaseModel):
    """Every filter the listing accepts. Unset means "do not narrow"."""

    platform: Annotated[
        list[str],
        Field(default_factory=list, max_length=MAX_CHOICES),
        AfterValidator(_known_platforms),
        Predicate(_on_platform),
    ]
    kind: Annotated[
        list[ItemKind],
        Field(default_factory=list, max_length=MAX_CHOICES),
        # An unclassified work has no kind, so it matches no choice of kinds.
        Predicate(lambda kinds, _: Work.item_kind.in_(kinds)),
    ]
    metacritic_min: Annotated[
        int | None,
        Field(default=None, ge=0, le=100),
        Predicate(lambda score, _: Work.metacritic_score >= score),
    ]
    metacritic_max: Annotated[
        int | None,
        Field(default=None, ge=0, le=100),
        Predicate(lambda score, _: Work.metacritic_score <= score),
    ]
    year_min: Annotated[
        int | None,
        Field(default=None, ge=1950, le=2100),
        Predicate(lambda year, _: Work.release_year >= year),
    ]
    year_max: Annotated[
        int | None,
        Field(default=None, ge=1950, le=2100),
        Predicate(lambda year, _: Work.release_year <= year),
    ]
    status: Annotated[
        list[PlayStatus],
        Field(default_factory=list, max_length=MAX_CHOICES),
        Predicate(lambda statuses, _: _status.in_([status.value for status in statuses])),
    ]
    hidden: Annotated[Hidden, Field(default=Hidden.EXCLUDE), Predicate(_on_hidden)]
    # Minutes, as `playtime_minutes` is everywhere else in the API.
    playtime_min: Annotated[
        int | None,
        Field(default=None, ge=0, le=MAX_MINUTES),
        Predicate(lambda minutes, _: _playtime >= minutes),
    ]
    playtime_max: Annotated[
        int | None,
        Field(default=None, ge=0, le=MAX_MINUTES),
        Predicate(lambda minutes, _: _playtime <= minutes),
    ]

    @model_validator(mode="after")
    def _ranges_are_ranges(self) -> Self:
        # Refused rather than answered with nothing: an empty page for
        # `year_min=2020&year_max=2010` reads as "you own no such games", which
        # is a claim about the library, not about the query.
        for name in RANGES:
            low, high = getattr(self, f"{name}_min"), getattr(self, f"{name}_max")
            if low is not None and high is not None and low > high:
                raise ValueError(f"{name}_min is greater than {name}_max")
        return self

    def predicates(self, user_id: int) -> Iterator[ColumnElement[bool]]:
        """One clause per filter that was set, in declaration order."""

        # The filters' own fields, not a subclass's: `ListingParams` adds the
        # page and the search, which are not filters and carry no predicate.
        for name, field in LibraryFilters.model_fields.items():
            value = getattr(self, name)
            if value is None or value == []:
                continue
            yield _predicate_of(name, field.metadata).build(value, user_id)


def _predicate_of(name: str, metadata: list[Any]) -> Predicate:
    found = [item for item in metadata if isinstance(item, Predicate)]
    if len(found) != 1:
        raise TypeError(f"filter `{name}` must declare exactly one Predicate")
    return found[0]
