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

A field may be a `Setting` instead: a value that changes what other filters
mean without narrowing anything itself, as `steam_reviews_min` sets how many
reviews a Steam score needs before a Steam range counts it.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import ColumnElement, false, func, select, true

from ludarium.enums import ItemKind, PlayStatus, ProviderKind
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    Genre,
    Provider,
    UserWorkState,
    Work,
    WorkGenre,
)
from ludarium.queries import owned_by
from ludarium.scores import DEFAULT_STEAM_REVIEWS, MAX_STEAM_REVIEWS, steam_score
from ludarium.seed import PROVIDER_SEED

# The widest a list parameter may be. Longer than any real selection — there
# are a dozen provider keys and eight kinds — and short enough that no caller
# builds an `IN (...)` near the bind limit.
MAX_CHOICES = 32
# A century of play, in minutes. Far past any library, and far inside the
# 64-bit integer SQLite binds: past that the driver raises and the answer is a
# 500 rather than a refusal.
MAX_MINUTES = 60 * 24 * 365 * 100

# The years the year filter accepts. A manual entry is held to the same, so
# that every year a user types in is one they can filter for.
FIRST_YEAR = 1950
LAST_YEAR = 2100

# The filters that come as a `_min` and a `_max` over one value.
RANGES = ("metacritic", "steam", "year", "playtime")


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
class Scope:
    """What a predicate may need beside its own value."""

    user_id: int
    # `LibraryFilters.steam_reviews_min`.
    steam_reviews: int


@dataclass(frozen=True, slots=True)
class Predicate:
    """What a filter's value means in SQL, given the library it narrows."""

    build: Callable[[Any, Scope], ColumnElement[bool]]


@dataclass(frozen=True, slots=True)
class Setting:
    """A field that narrows nothing itself; the predicates read it from their `Scope`."""


def _on_platform(keys: list[str], scope: Scope) -> ColumnElement[bool]:
    # A live copy, by the same `owned_by` the listing itself uses: a work kept
    # by its Steam copy does not match GOG because its GOG copy was removed.
    return (
        select(EntitlementWork.work_id)
        .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
        .join(Account, Account.id == Entitlement.account_id)
        .join(Provider, Provider.id == Account.provider_id)
        .where(EntitlementWork.work_id == Work.id, *owned_by(scope.user_id), Provider.key.in_(keys))
        .exists()
    )


# What the listing shows for a work with no state row (`_describe`), so each
# filter agrees with what is printed on the card.
_playtime = func.coalesce(UserWorkState.playtime_minutes, 0)
_status = func.coalesce(UserWorkState.play_status, PlayStatus.NOT_STARTED.value)
_hidden = func.coalesce(UserWorkState.is_hidden, false())


def _on_steam(
    bound: Callable[[ColumnElement[Any], int], ColumnElement[bool]],
) -> Predicate:
    """A bound on the Steam score, counted only over enough reviews (`ludarium.scores`).

    100% of three reviews is not a better game than 94% of fifty thousand, so a
    score under the threshold matches no range, as a work with no score at all
    does. The same expression orders the listing, so the filter and the sort
    agree on which scores count.
    """

    return Predicate(lambda percent, scope: bound(steam_score(scope.steam_reviews), percent))


# IGDB's slug shape: lowercase words joined by hyphens, "role-playing-rpg".
GENRE_SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"


def _in_genre(slugs: list[str], _: Scope) -> ColumnElement[bool]:
    # A work with no genres — every work IGDB has not matched — is in none of
    # them, which is what EXISTS says of it without a word.
    return (
        select(WorkGenre.work_id)
        .join(Genre, Genre.id == WorkGenre.genre_id)
        .where(WorkGenre.work_id == Work.id, Genre.slug.in_(slugs))
        .exists()
    )


class Hidden(StrEnum):
    """Whether the works the user hid are in the listing."""

    # The default: hidden means "not in my library view", which is the point of
    # hiding something. The work stays owned and counted; only the view skips it.
    EXCLUDE = "exclude"
    INCLUDE = "include"
    ONLY = "only"


def _on_hidden(hidden: Hidden, _: Scope) -> ColumnElement[bool]:
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
    # Any of them. A slug no work has matches nothing, as a kind would: unlike
    # a platform, the set of genres is data rather than code, and a genre IGDB
    # retires should not make an old link fail.
    genre: Annotated[
        list[Annotated[str, StringConstraints(max_length=64, pattern=GENRE_SLUG)]],
        Field(default_factory=list, max_length=MAX_CHOICES),
        Predicate(_in_genre),
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
    # Percent positive, over at least `steam_reviews_min` reviews (`_on_steam`).
    steam_min: Annotated[
        int | None,
        Field(default=None, ge=0, le=100),
        _on_steam(lambda score, percent: score >= percent),
    ]
    steam_max: Annotated[
        int | None,
        Field(default=None, ge=0, le=100),
        _on_steam(lambda score, percent: score <= percent),
    ]
    # How many reviews a Steam score needs to count, in the ranges above and in
    # the Steam order. Under it the score is no score; the work stays listed.
    steam_reviews_min: Annotated[
        int,
        Field(default=DEFAULT_STEAM_REVIEWS, ge=1, le=MAX_STEAM_REVIEWS),
        Setting(),
    ]
    year_min: Annotated[
        int | None,
        Field(default=None, ge=FIRST_YEAR, le=LAST_YEAR),
        Predicate(lambda year, _: Work.release_year >= year),
    ]
    year_max: Annotated[
        int | None,
        Field(default=None, ge=FIRST_YEAR, le=LAST_YEAR),
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

        scope = Scope(user_id=user_id, steam_reviews=self.steam_reviews_min)
        # The filters' own fields, not a subclass's: `ListingParams` adds the
        # page and the search, which are not filters and carry no predicate.
        for name, field in LibraryFilters.model_fields.items():
            value = getattr(self, name)
            predicate = _predicate_of(name, field.metadata)
            if predicate is None or value is None or value == []:
                continue
            yield predicate.build(value, scope)


def _predicate_of(name: str, metadata: list[Any]) -> Predicate | None:
    """The field's predicate, or None for a setting. Exactly one of the two."""

    found = [item for item in metadata if isinstance(item, Predicate | Setting)]
    if len(found) != 1:
        raise TypeError(f"filter `{name}` must declare exactly one Predicate or Setting")
    return found[0] if isinstance(found[0], Predicate) else None
