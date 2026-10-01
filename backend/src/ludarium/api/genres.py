"""The genres in the user's library, for the filter panel to offer (#92)."""

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.models import Entitlement, EntitlementWork, Genre, WorkGenre
from ludarium.queries import owned_by

router = APIRouter(prefix="/genres", tags=["genres"])


class GenreSummary(BaseModel):
    # The filter's value, and the key the UI translates the name by.
    slug: str
    # IGDB's, in English: what the UI shows where it has no translation.
    name: str


@router.get("")
async def in_library(session: SessionDep, record: CurrentSession) -> list[GenreSummary]:
    """Every genre of a work the user still owns a copy of, by name.

    Only these: a genre offered in the panel that matches nothing in the
    library would be a filter that can only ever answer with nothing.
    """

    owned = (
        select(EntitlementWork.work_id)
        .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
        .where(EntitlementWork.work_id == WorkGenre.work_id, *owned_by(record.user_id))
    )
    rows = await session.execute(
        select(Genre.slug, Genre.name)
        .join(WorkGenre, WorkGenre.genre_id == Genre.id)
        .where(owned.exists())
        .distinct()
        .order_by(Genre.name, Genre.slug)
    )
    return [GenreSummary(slug=slug, name=name) for slug, name in rows]
