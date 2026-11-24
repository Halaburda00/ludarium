"""The built frontend, served by the same process as the API (#143).

One container runs one process, so FastAPI serves `frontend/dist` itself rather
than leaving it to a second server. In development nothing is mounted and
`pnpm dev` proxies `/api` here, as before.
"""

from pathlib import Path
from typing import Final

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse

from ludarium.config import ConfigurationError

INDEX: Final = "index.html"
# Vite names everything under `assets/` by its content hash, so a file there
# never changes under one URL and may be kept for as long as a browser likes.
ASSETS: Final = "assets"
IMMUTABLE: Final = "public, max-age=31536000, immutable"
# Revalidated on every load: `index.html` names the current hashed bundles, and
# a stale one would ask for bundles a new release no longer has.
REVALIDATE: Final = "no-cache"


def mount_frontend(app: FastAPI, directory: Path) -> None:
    """Serve `directory` for every path the API does not claim.

    Registered last, so every API route matches first. Left out of the OpenAPI
    contract, which describes the API, and out of `current_session`: the login
    page has to load before anyone is signed in.
    """

    root = directory.resolve()
    if not (root / INDEX).is_file():
        raise ConfigurationError(
            f"LUDARIUM_FRONTEND_DIR is {directory}, which has no {INDEX}; "
            "build it with `pnpm run build` or leave the setting unset"
        )

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def frontend(path: str) -> FileResponse:
        # An API path nothing answered is the API's 404, never the page: a
        # client calling a route that does not exist should not get HTML back.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        file = (root / path).resolve()
        if path and file.is_relative_to(root) and file.is_file():
            cache = IMMUTABLE if file.parent == root / ASSETS else REVALIDATE
            return FileResponse(file, headers={"Cache-Control": cache})
        # Any other path is a route of the single-page app, so a reload on
        # `/library?status=queued` gets the page that knows how to draw it.
        return FileResponse(root / INDEX, headers={"Cache-Control": REVALIDATE})
