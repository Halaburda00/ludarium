"""The image files the instance keeps, served from its own disk (#51).

Served rather than linked to IGDB, so the library does not depend on a third
party being up and does not tell it what the user is looking at. Behind the
session like everything else: the files are IGDB's, and they are not published.
"""

import mimetypes
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.models import ImageAsset

# The row names one file for good — a replaced cover is a new row, under a new
# id that is never reused — so a browser may keep it and never ask again.
CACHE_CONTROL: Final = "private, max-age=31536000, immutable"

router = APIRouter(prefix="/images", tags=["images"])


@router.get(
    "/{image_id}",
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}}, "description": "The image file"}},
)
async def image(
    image_id: int, request: Request, session: SessionDep, record: CurrentSession
) -> FileResponse:
    asset = await session.get(ImageAsset, image_id)
    if asset is None or asset.local_path is None or asset.fetched_at is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such image")
    data_dir: Path = request.app.state.settings.data_dir.resolve()
    path = (data_dir / asset.local_path).resolve()
    # The paths are ours, written by the cover step. Checked all the same: a
    # row is the one thing here that decides which file leaves the disk.
    if not path.is_relative_to(data_dir) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such image")
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": CACHE_CONTROL},
    )
