"""Saved views: a name on a library query, kept so it can be run again.

A view stores the query string and never the works it matched, so opening one
re-runs it against the library as it is now. The string is read through the
listing's own parameter model, the same one a live request is validated by.

What the listing no longer understands — a filter that was removed, a platform
dropped from the seed, a sort that was renamed — is left out when the view is
read, and named in `dropped`. A view that fails to open over one stale part
would lose the rest of it with it.
"""

from collections.abc import Iterator, Mapping
from typing import Annotated, Any, Final, get_origin
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ludarium.api.works import ListingParams
from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.filters import RANGES, LibraryFilters
from ludarium.models import SavedView

# Room for a sentence, not for a paragraph: it is a label in a list.
MAX_NAME: Final = 100
# Longer than every filter set at once; far shorter than a request body worth refusing.
MAX_QUERY: Final = 2000
# What a view holds: the filters and the order. Not the search, which is what
# someone is looking for now rather than a way of looking at the library, and
# not the page or the cursor, which are positions in one answer.
VIEW_KEYS: Final = (*LibraryFilters.model_fields, "sort", "order")

router = APIRouter(prefix="/views", tags=["views"])

type Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME)
]


class ViewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    # The listing's query string, filters and order only.
    query: str = Field(max_length=MAX_QUERY)


class ViewRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name


class ViewOrder(BaseModel):
    """Every view's id, in the order they should be listed."""

    model_config = ConfigDict(extra="forbid")

    ids: list[int]


class SavedViewResponse(BaseModel):
    id: int
    name: str
    position: int
    # What the view asks for, as the listing's query string, with anything the
    # listing no longer takes left out.
    query: str
    # What was left out, as `key=value`, so the client can say what changed
    # rather than open a different view than the one that was saved.
    dropped: list[str]


def read_query(query: str) -> tuple[ListingParams, list[str]]:
    """The query as the listing would take it, and every part of it that it would not.

    Each value is judged on its own, so one unknown platform drops that platform
    and not the other two beside it. A range whose ends contradict each other is
    dropped whole: neither end means what it did.
    """

    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, values in parse_qs(query, keep_blank_values=True).items():
        if key not in VIEW_KEYS:
            dropped += _pairs(key, values)
        elif _is_list(key):
            # Once each: a value repeated says nothing more, and counted every
            # time it would overflow the list's bound with no two values that
            # differ. Every list takes one of a few known values, so what is
            # left is always inside it.
            for value in dict.fromkeys(values):
                if _accepts({key: [value]}):
                    kept.setdefault(key, []).append(value)
                else:
                    dropped += _pairs(key, [value])
        elif len(values) == 1 and _accepts({key: values[0]}):
            kept[key] = values[0]
        else:
            dropped += _pairs(key, values)
    for name in RANGES:
        ends = {key: kept[key] for key in (f"{name}_min", f"{name}_max") if key in kept}
        if not _accepts(ends):
            for key, value in ends.items():
                dropped += _pairs(key, [value])
                del kept[key]
    return ListingParams.model_validate(kept), dropped


def write_query(params: ListingParams) -> str:
    """The view's parameters as a query string, in declaration order, defaults left out."""

    return urlencode(list(_set_pairs(params)))


def _set_pairs(params: ListingParams) -> Iterator[tuple[str, str]]:
    for key in VIEW_KEYS:
        value = getattr(params, key)
        if value == ListingParams.model_fields[key].get_default(call_default_factory=True):
            continue
        # `str` of a `StrEnum` is its value, which is what the listing takes.
        for item in value if isinstance(value, list) else [value]:
            yield key, str(item)


def _is_list(key: str) -> bool:
    return get_origin(ListingParams.model_fields[key].annotation) is list


def _accepts(params: Mapping[str, Any]) -> bool:
    try:
        ListingParams.model_validate(params)
    except ValidationError:
        return False
    return True


def _pairs(key: str, values: list[str]) -> list[str]:
    return [urlencode({key: value}) for value in values]


def _describe(view: SavedView) -> SavedViewResponse:
    params, dropped = read_query(view.query)
    return SavedViewResponse(
        id=view.id,
        name=view.name,
        position=view.position,
        query=write_query(params),
        dropped=dropped,
    )


async def _views(session: SessionDep, user_id: int) -> list[SavedView]:
    return list(
        await session.scalars(
            select(SavedView)
            .where(SavedView.user_id == user_id)
            .order_by(SavedView.position, SavedView.id)
        )
    )


async def _view_or_404(session: SessionDep, view_id: int, user_id: int) -> SavedView:
    view = await session.get(SavedView, view_id)
    # Another user's view is answered as one that does not exist.
    if view is None or view.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such view")
    return view


async def _commit_named(session: SessionDep, name: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"there is already a view named “{name}”"
        ) from exc


@router.get("")
async def listing(session: SessionDep, record: CurrentSession) -> list[SavedViewResponse]:
    return [_describe(view) for view in await _views(session, record.user_id)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(
    payload: ViewCreate, session: SessionDep, record: CurrentSession
) -> SavedViewResponse:
    """Saved at the end of the list, in the canonical form it is read back in.

    Stricter than reading: what is being saved now should be all understood
    now, so anything that would be dropped is refused instead. Dropping it here
    would save a different view than the one on the screen, without a word.
    """

    params, dropped = read_query(payload.query)
    if dropped:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"not a filter or an order the library takes: {', '.join(dropped)}",
        )
    last = await session.scalar(
        select(func.max(SavedView.position)).where(SavedView.user_id == record.user_id)
    )
    view = SavedView(
        user_id=record.user_id,
        name=payload.name,
        query=write_query(params),
        position=0 if last is None else last + 1,
    )
    session.add(view)
    await _commit_named(session, payload.name)
    return _describe(view)


@router.patch("/{view_id}")
async def rename(
    view_id: int, payload: ViewRename, session: SessionDep, record: CurrentSession
) -> SavedViewResponse:
    view = await _view_or_404(session, view_id, record.user_id)
    view.name = payload.name
    await _commit_named(session, payload.name)
    return _describe(view)


@router.put("/order")
async def reorder(
    payload: ViewOrder, session: SessionDep, record: CurrentSession
) -> list[SavedViewResponse]:
    """Every view, each once. Anything else is a list from before a view was added or deleted.

    A 409 rather than a best effort: placing the views it names and leaving
    the rest wherever they fall would be an order nobody chose.
    """

    views = {view.id: view for view in await _views(session, record.user_id)}
    if sorted(payload.ids) != sorted(views):
        raise HTTPException(status.HTTP_409_CONFLICT, "the order must name every view exactly once")
    for position, view_id in enumerate(payload.ids):
        views[view_id].position = position
    await session.commit()
    return [_describe(views[view_id]) for view_id in payload.ids]


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(view_id: int, session: SessionDep, record: CurrentSession) -> Response:
    view = await _view_or_404(session, view_id, record.user_id)
    await session.delete(view)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
