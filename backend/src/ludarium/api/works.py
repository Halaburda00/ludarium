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
from datetime import datetime
from typing import Annotated, Final
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import literal, select, tuple_

from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.enums import ItemKind, PlayStatus
from ludarium.models import (
    Account,
    Entitlement,
    EntitlementWork,
    Provider,
    UserWorkState,
    Work,
)
from ludarium.queries import owned_by

DEFAULT_LIMIT: Final = 100
MAX_LIMIT: Final = 500
MAX_CURSOR: Final = 256

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


class WorkSummary(BaseModel):
    id: int
    title: str
    sort_title: str
    is_matched: bool
    item_kind: ItemKind
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
    entitlements: list[EntitlementSummary]


class WorksPage(BaseModel):
    works: list[WorkSummary]
    # Null on the last page. Opaque on purpose: the key it encodes is ours to
    # change without asking every client to change with it.
    next_cursor: str | None


def _cursor(work: Work) -> str:
    return urlsafe_b64encode(json.dumps([work.sort_title, work.id]).encode()).decode()


def _after(cursor: str) -> tuple[str, int]:
    try:
        decoded = json.loads(urlsafe_b64decode(cursor.encode()))
        match decoded:
            # Types checked rather than coerced. `str()` and `int()` accept
            # almost anything, and `int(3.7)` silently becomes 3 — a made-up
            # cursor would then page from somewhere nobody chose, which is worse
            # than being refused. `bool` is an `int` and is excluded by name.
            case [str() as sort_title, int() as work_id] if not isinstance(work_id, bool):
                return sort_title, work_id
        raise ValueError("a cursor is a title and an id")
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
) -> WorksPage:
    """One page, keyed on `(sort_title, id)` rather than an offset.

    An offset re-reads and discards every row before the page, so the last page
    of a large library costs the most; and a sync landing a new title mid-scroll
    shifts every later page by one, which shows up as a duplicated or skipped
    row. A keyset does neither: `ix_work_sort_title_id` seeks straight to the
    position and the page is defined by content rather than by count.
    """

    user_id = record.user_id
    owned = (
        select(EntitlementWork.work_id)
        .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
        .where(EntitlementWork.work_id == Work.id, *owned_by(user_id))
    )
    page = (
        select(Work, UserWorkState)
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
        .order_by(Work.sort_title, Work.id)
        # One more than asked for, so "is there a next page" is answered without
        # a count and without handing the client an empty page to discover it.
        .limit(limit + 1)
    )
    if cursor is not None:
        sort_title, work_id = _after(cursor)
        page = page.where(
            tuple_(Work.sort_title, Work.id) > tuple_(literal(sort_title), literal(work_id))
        )

    rows = list(await session.execute(page))
    has_more = len(rows) > limit
    rows = rows[:limit]

    copies = await _entitlements(session, [work.id for work in (work for work, _ in rows)], user_id)
    # A work with nothing live pointing at it is dropped rather than shown
    # empty-handed: a row that contradicts the endpoint's own rule — in the list
    # because something live points at it, with nothing listed — is worse than a
    # page one short.
    #
    # Unreachable on SQLite since ADR-0016 made the two queries one snapshot,
    # and not on PostgreSQL, whose default READ COMMITTED gives each statement
    # its own. ADR-0004 keeps PostgreSQL a supported target, so the defence
    # stays and its test forces the race rather than waiting for it.
    works = [(work, state) for work, state in rows if copies.get(work.id)]
    return WorksPage(
        works=[_describe(work, state, copies[work.id]) for work, state in works],
        # From the last row read, not the last row kept, so the listing always
        # advances. Taken from the last kept row it would re-read whatever was
        # dropped — harmless — but a page where *everything* was dropped would
        # have no last kept row and no cursor, and the client would stop with
        # the rest of the library unread. A row skipped at a page boundary shows
        # up again on the next refresh; a truncated library does not.
        next_cursor=_cursor(rows[-1][0]) if has_more else None,
    )


def _describe(
    work: Work, state: UserWorkState | None, copies: list[EntitlementSummary]
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
        entitlements=copies,
    )


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
