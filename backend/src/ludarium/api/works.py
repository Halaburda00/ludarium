"""The read side of the milestone: one page of the library, work-centric.

Work-centric because ADR-0015 makes it so from the first run — every entitlement
has a work the moment it is synced, so the grid never has two shapes of row.

`removed_at IS NULL` is on the entitlement, not the work, and that is the whole
of rule 1 seen from here: a work is in the list because something live points at
it. When the last live entitlement goes, the work leaves the list without
anything having been deleted, and comes back if the entitlement is restored.
"""

import binascii
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence
from datetime import date, datetime
from typing import Annotated, Final
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, literal, select, tuple_

from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.enums import CompanyRole, EntityType, ImageKind, ItemKind, PlayStatus, SteamRating
from ludarium.models import (
    Account,
    Company,
    Entitlement,
    EntitlementWork,
    ExternalId,
    ImageAsset,
    Provider,
    UserWorkState,
    Work,
    WorkCompany,
)
from ludarium.queries import owned_by
from ludarium.titles import search_key

DEFAULT_LIMIT: Final = 100
MAX_LIMIT: Final = 500
MAX_CURSOR: Final = 256
# Longer than any title, so nothing a person types is refused.
MAX_QUERY: Final = 200
# Carried inside every cursor, so one issued under an older ordering is refused
# rather than read under this one. The cursors before it were `[sort_title, id]`
# — the same shape as `[sort_key, id]`, and a position in a different order.
# Accepted, one would page from wherever its title happened to compare, which is
# the made-up cursor `_after` exists to turn away.
CURSOR_VERSION: Final = 2
# Where Metacritic scores come from, and whose page each one links to.
SCORE_SOURCE: Final = "rawg"
# Whose store page a Steam review score links to, and where on it the reviews
# are: the anchor the store's own "All Reviews" link uses.
STEAM: Final = "steam"
REVIEWS_ANCHOR: Final = "#app_reviews_hash"
# Who is named first on a work's page: whoever put it out, then whoever made it.
CREDIT_ORDER: Final = (
    CompanyRole.PUBLISHER,
    CompanyRole.DEVELOPER,
    CompanyRole.PORTING,
    CompanyRole.SUPPORT,
)

router = APIRouter(prefix="/works", tags=["works"])


class EntitlementSummary(BaseModel):
    """One copy the user owns, and where it came from. The platform column of the table."""

    id: int
    provider: str
    provider_name: str
    provider_item_id: str | None
    # The platform's own name for it, which is not `work.title`: they are
    # different fields, not competing values for one (rule 5).
    provider_title: str
    playtime_minutes: int | None
    store_url: str | None


class Score(BaseModel):
    """A Metacritic score and the page of the source it came from, which is RAWG.

    One object so the two cannot be separated: RAWG's terms require an active
    link wherever its data is shown, and a score the API hands out without its
    link is a score some client will show without it.
    """

    value: int
    source_name: str
    source_url: str


class SteamReviews(BaseModel):
    """The Steam store's verdict on one of the work's apps, and where to read the reviews.

    `rating` is the verdict's value rather than Steam's label, which the store
    translates: the client names it in the user's language.
    """

    rating: SteamRating
    percent: int
    count: int
    url: str


class Cover(BaseModel):
    """A cover at two sizes, for `srcset`: `url` at its own size, `url_2x` at twice it."""

    url: str
    url_2x: str | None
    # Of `url`. Lets the grid reserve the space before the image arrives.
    width: int
    height: int


class WorkSummary(BaseModel):
    id: int
    title: str
    sort_title: str
    is_matched: bool
    # None until something has classified it; not the same answer as `game`.
    item_kind: ItemKind | None
    release_year: int | None
    play_status: PlayStatus
    is_favourite: bool
    # Hidden is returned, not applied: "excluded from the default grid" is a
    # filter the grid owns (M3), and a list that quietly drops rows is worse
    # than one that says which rows are marked.
    is_hidden: bool
    # The sum across this work's entitlements, resolved (rule 5).
    playtime_minutes: int
    last_played_at: datetime | None
    # Null where there is no score, and where there is one but nothing to link
    # it to: a score without its attribution is not shown at all.
    metacritic: Score | None
    # Null where the store has no verdict, or none has been asked for yet.
    steam_reviews: SteamReviews | None
    # Null until the cover step has fetched one.
    cover: Cover | None
    entitlements: list[EntitlementSummary]


class Credit(BaseModel):
    """One company and every role it played in the work."""

    name: str
    roles: list[CompanyRole]


class WorkDetail(WorkSummary):
    """One work with what the grid leaves out: its summary, date and who made it (#54)."""

    summary: str | None
    release_date: date | None
    # Publishers first, then developers, porting and support studios
    # (`CREDIT_ORDER`); a company in several roles is listed once.
    companies: list[Credit]


class WorksPage(BaseModel):
    works: list[WorkSummary]
    # Null on the last page. Opaque on purpose: the key it encodes is ours to
    # change without asking every client to change with it.
    next_cursor: str | None


def _cursor(work: Work) -> str:
    return urlsafe_b64encode(json.dumps([CURSOR_VERSION, work.sort_key, work.id]).encode()).decode()


def _after(cursor: str) -> tuple[str, int]:
    try:
        decoded = json.loads(urlsafe_b64decode(cursor.encode()))
        match decoded:
            # Types checked rather than coerced. `str()` and `int()` accept
            # almost anything, and `int(3.7)` silently becomes 3 — a made-up
            # cursor would then page from somewhere nobody chose, which is worse
            # than being refused. `bool` is an `int` and is excluded by name.
            case [int() as version, str() as key, int() as work_id] if (
                version == CURSOR_VERSION and not isinstance(work_id, bool)
            ):
                return key, work_id
        raise ValueError("a cursor is a version, a key and an id")
    except (ValueError, TypeError, binascii.Error) as exc:
        # No detail about what was wrong with it: a cursor is ours, and a client
        # that made one up has nothing to learn from the answer.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "that is not a cursor this API issued"
        ) from exc


def _store_url(template: str | None, provider_item_id: str | None) -> str | None:
    """Built here rather than stored: the template is seeded from code and may change.

    We never launch a game, so the store page is the answer to "where do I find
    this". Quoted, because an id is a provider's string and only Steam's happen
    to be numeric.
    """

    if not template or not provider_item_id:
        return None
    return template.replace("{id}", quote(provider_item_id, safe=""))


def _summarise(entitlement: Entitlement, provider: Provider) -> EntitlementSummary:
    return EntitlementSummary(
        id=entitlement.id,
        provider=provider.key,
        provider_name=provider.display_name,
        provider_item_id=entitlement.provider_item_id,
        provider_title=entitlement.provider_title,
        playtime_minutes=entitlement.playtime_minutes,
        store_url=_store_url(provider.store_url_template, entitlement.provider_item_id),
    )


@router.get("")
async def listing(
    session: SessionDep,
    record: CurrentSession,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    # Bounded like every other input a caller controls: the cursor is decoded
    # before it is judged, and there is no reason to decode a megabyte first.
    cursor: Annotated[str | None, Query(max_length=MAX_CURSOR)] = None,
    q: Annotated[str | None, Query(max_length=MAX_QUERY)] = None,
) -> WorksPage:
    """One page, keyed on `(sort_key, id)` rather than an offset.

    An offset re-reads and discards every row before the page, so the last page
    of a large library costs the most; and a sync landing a new title mid-scroll
    shifts every later page by one, which shows up as a duplicated or skipped
    row. A keyset does neither: `ix_work_sort_key_id` seeks straight to the
    position and the page is defined by content rather than by count.

    The key and not `sort_title` itself, because the database compares bytes:
    "ARC Raiders" would file ahead of "Amnesia", and a trademark sign would split
    one series into two blocks (ADR-0018).

    `q` narrows the listing without changing its order or its cursor: a
    filtered page is still keyed on `(sort_key, id)`, so a search pages as the
    library does.
    """

    user_id = record.user_id
    page = (
        _owned_works(user_id)
        .order_by(Work.sort_key, Work.id)
        # One more than asked for, so "is there a next page" is answered without
        # a count and without handing the client an empty page to discover it.
        .limit(limit + 1)
    )
    wanted = search_key(q) if q is not None else ""
    if wanted:
        page = page.where(_matches(wanted, user_id))
    if cursor is not None:
        key, work_id = _after(cursor)
        page = page.where(tuple_(Work.sort_key, Work.id) > tuple_(literal(key), literal(work_id)))

    rows = list((await session.execute(page)).tuples())
    has_more = len(rows) > limit
    rows = rows[:limit]

    return WorksPage(
        works=await _summaries(session, rows, user_id),
        # From the last row read, not the last row kept, so the listing always
        # advances. Taken from the last kept row it would re-read whatever was
        # dropped — harmless — but a page where *everything* was dropped would
        # have no last kept row and no cursor, and the client would stop with
        # the rest of the library unread. A row skipped at a page boundary shows
        # up again on the next refresh; a truncated library does not.
        next_cursor=_cursor(rows[-1][0]) if has_more else None,
    )


def _matches(wanted: str, user_id: int) -> ColumnElement[bool]:
    """A work whose title holds `wanted`, or one of whose live copies' store names does.

    Both folded as the sort key is, so a result set is matched by the rule it
    is ordered by (#53). The store's name counts because it is a different
    field on purpose (rule 5): someone searching for the name they saw in
    Steam should find the game under IGDB's. A removed copy's name does not,
    as it does not put the work in the list either.

    A substring, not a word prefix: "itcher" finds The Witcher. Escaped, so a
    `%` or `_` in the query is a character to find rather than a wildcard.
    """

    named = (
        select(EntitlementWork.work_id)
        .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
        .where(
            EntitlementWork.work_id == Work.id,
            *owned_by(user_id),
            Entitlement.provider_title_key.contains(wanted, autoescape=True),
        )
    )
    return Work.title_key.contains(wanted, autoescape=True) | named.exists()


@router.get("/{work_id}")
async def detail(work_id: int, session: SessionDep, record: CurrentSession) -> WorkDetail:
    """One work, if it is in the user's library.

    404 for a work that does not exist and for one only removed copies point
    at, alike: the listing would not show it, so neither does this, and the
    answer does not say which of the two it was.
    """

    rows = list(
        (await session.execute(_owned_works(record.user_id).where(Work.id == work_id))).tuples()
    )
    summaries = await _summaries(session, rows, record.user_id)
    if not summaries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such work in the library")
    work = rows[0][0]
    return WorkDetail(
        **summaries[0].model_dump(),
        summary=work.summary,
        release_date=work.release_date,
        companies=await _credits(session, work_id),
    )


async def _credits(session: SessionDep, work_id: int) -> list[Credit]:
    order = {role: rank for rank, role in enumerate(CREDIT_ORDER)}
    roles: dict[int, tuple[str, set[CompanyRole]]] = {}
    for company_id, name, role in await session.execute(
        select(Company.id, Company.name, WorkCompany.role)
        .join(WorkCompany, WorkCompany.company_id == Company.id)
        .where(WorkCompany.work_id == work_id)
    ):
        roles.setdefault(company_id, (name, set()))[1].add(role)
    credits = [
        Credit(name=name, roles=sorted(held, key=order.__getitem__))
        for name, held in roles.values()
    ]
    # By the company's most prominent role, then by name, so the order does not
    # depend on which row the database returned first.
    return sorted(credits, key=lambda credit: (order[credit.roles[0]], credit.name.casefold()))


type Row = tuple[Work, UserWorkState | None, str | None]


def _owned_works(user_id: int) -> Select[Row]:
    """Every work the user still owns a copy of, with their state and its RAWG slug.

    The listing orders and pages this; the detail view picks one row from it,
    so "in the library" means the same thing to both.
    """

    owned = (
        select(EntitlementWork.work_id)
        .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
        .where(EntitlementWork.work_id == Work.id, *owned_by(user_id))
    )
    # A subquery rather than a join: nothing in the schema stops a work holding
    # two slugs, and a join would then list the work twice.
    slug = (
        select(ExternalId.value)
        .where(
            ExternalId.entity_type == EntityType.WORK,
            ExternalId.entity_id == Work.id,
            ExternalId.namespace == SCORE_SOURCE,
        )
        .order_by(ExternalId.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(Work, UserWorkState, slug)
        # Outer, because "every work reachable by a live entitlement has a
        # `user_work_state` row" is a convention `sync._stub` keeps and no
        # constraint enforces. An inner join makes a future write path that
        # forgets it — a manual entry, the M2 matcher — drop games from the
        # library with no error anywhere. A missing row shows the work with its
        # defaults instead, which is both recoverable and visible.
        .outerjoin(
            UserWorkState,
            (UserWorkState.work_id == Work.id) & (UserWorkState.user_id == user_id),
        )
        .where(owned.exists())
    )


async def _summaries(session: SessionDep, rows: Sequence[Row], user_id: int) -> list[WorkSummary]:
    """The rows as the API describes them, in a fixed number of queries however many there are."""

    copies = await _entitlements(session, [work.id for work, _, _ in rows], user_id)
    source = await session.scalar(select(Provider).where(Provider.key == SCORE_SOURCE))
    steam = await session.scalar(select(Provider).where(Provider.key == STEAM))
    covers = await _covers(session, [work.id for work, _, _ in rows])
    # A work with nothing live pointing at it is dropped rather than shown
    # empty-handed: a row that contradicts the endpoint's own rule — in the list
    # because something live points at it, with nothing listed — is worse than a
    # page one short.
    #
    # Unreachable on SQLite since ADR-0016 made the two queries one snapshot,
    # and not on PostgreSQL, whose default READ COMMITTED gives each statement
    # its own. ADR-0004 keeps PostgreSQL a supported target, so the defence
    # stays and its test forces the race rather than waiting for it.
    return [
        _describe(
            work,
            state,
            copies[work.id],
            _score(work, source, held),
            _steam_reviews(work, steam),
            covers.get(work.id),
        )
        for work, state, held in rows
        if copies.get(work.id)
    ]


def _score(work: Work, source: Provider | None, slug: str | None) -> Score | None:
    url = _store_url(source.store_url_template, slug) if source is not None else None
    if work.metacritic_score is None or source is None or url is None:
        return None
    return Score(value=work.metacritic_score, source_name=source.display_name, source_url=url)


def _steam_reviews(work: Work, steam: Provider | None) -> SteamReviews | None:
    rating, percent, count = (
        work.steam_review_rating,
        work.steam_review_percent,
        work.steam_review_count,
    )
    page = _store_url(steam.store_url_template, work.steam_review_appid) if steam else None
    # All four or nothing: the step writes them together, and a user overriding
    # one without the others leaves a verdict that no longer names its app.
    if rating is None or percent is None or count is None or page is None:
        return None
    return SteamReviews(rating=rating, percent=percent, count=count, url=page + REVIEWS_ANCHOR)


def _describe(
    work: Work,
    state: UserWorkState | None,
    copies: list[EntitlementSummary],
    metacritic: Score | None,
    steam_reviews: SteamReviews | None,
    cover: Cover | None,
) -> WorkSummary:
    return WorkSummary(
        id=work.id,
        title=work.title,
        sort_title=work.sort_title,
        is_matched=work.is_matched,
        item_kind=work.item_kind,
        release_year=work.release_year,
        # The defaults the missing row would have carried. Spelled out because a
        # transient `UserWorkState()` would not have them: SQLAlchemy applies
        # column defaults on flush, not on construction.
        play_status=state.play_status if state else PlayStatus.NOT_STARTED,
        is_favourite=state.is_favourite if state else False,
        is_hidden=state.is_hidden if state else False,
        playtime_minutes=state.playtime_minutes if state else 0,
        last_played_at=state.last_played_at if state else None,
        metacritic=metacritic,
        steam_reviews=steam_reviews,
        cover=cover,
        entitlements=copies,
    )


async def _covers(session: SessionDep, work_ids: list[int]) -> dict[int, Cover]:
    """One query for the page. The narrowest file is `url`; one twice its width, `url_2x`."""

    if not work_ids:
        return {}
    files: dict[int, list[ImageAsset]] = {}
    for image in await session.scalars(
        select(ImageAsset)
        .where(
            ImageAsset.entity_type == EntityType.WORK,
            ImageAsset.entity_id.in_(work_ids),
            ImageAsset.kind == ImageKind.COVER,
            ImageAsset.fetched_at.is_not(None),
            ImageAsset.width.is_not(None),
            ImageAsset.height.is_not(None),
        )
        .order_by(ImageAsset.entity_id, ImageAsset.width, ImageAsset.id)
    ):
        files.setdefault(image.entity_id, []).append(image)
    covers: dict[int, Cover] = {}
    for work_id, images in files.items():
        base = images[0]
        # Not null by the query; mypy cannot see that.
        width, height = base.width or 0, base.height or 0
        sharp = next((image for image in images if image.width == 2 * width), None)
        covers[work_id] = Cover(
            url=_image_url(base),
            url_2x=_image_url(sharp) if sharp is not None else None,
            width=width,
            height=height,
        )
    return covers


def _image_url(image: ImageAsset) -> str:
    return f"/api/images/{image.id}"


async def _entitlements(
    session: SessionDep, work_ids: list[int], user_id: int
) -> dict[int, list[EntitlementSummary]]:
    """One query for the whole page, not one per work.

    Same `removed_at IS NULL` as the page itself: a work kept by its Steam copy
    must not list the GOG copy that was removed last week.
    """

    if not work_ids:
        return {}
    rows = await session.execute(
        select(EntitlementWork.work_id, Entitlement, Provider)
        .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
        .join(Account, Account.id == Entitlement.account_id)
        .join(Provider, Provider.id == Account.provider_id)
        .where(EntitlementWork.work_id.in_(work_ids), *owned_by(user_id))
        .order_by(Provider.key, Entitlement.id)
    )
    grouped: dict[int, list[EntitlementSummary]] = {}
    for work_id, entitlement, provider in rows:
        grouped.setdefault(work_id, []).append(_summarise(entitlement, provider))
    return grouped
