"""The built frontend, served by the same process as the API (#143).

One container runs one process, so FastAPI serves `frontend/dist` itself rather
than leaving it to a second server. In development nothing is mounted and
`pnpm dev` proxies `/api` here, as before.
"""

from pathlib import Path
from typing import Final

from fastapi import FastAPI
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from ludarium.config import ConfigurationError

INDEX: Final = "index.html"
# Vite names everything under `assets/` by its content hash, so a file there
# never changes under one URL and may be kept for as long as a browser likes.
ASSETS: Final = "assets"
IMMUTABLE: Final = "public, max-age=31536000, immutable"
# Revalidated on every load: `index.html` names the current hashed bundles, and
# a stale one would ask for bundles a new release no longer has.
REVALIDATE: Final = "no-cache"
READS: Final = frozenset({"GET", "HEAD"})


def mount_frontend(app: FastAPI, directory: Path) -> None:
    """Serve `directory` for every request no API route matches.

    As the router's default rather than a catch-all route: the default runs
    only once no route matched even by path, so the API keeps its own 404 for
    an unknown path and its 405 for a known one asked with the wrong method. Not
    a route, it is also outside the OpenAPI contract and `current_session`: the
    login page has to load before anyone is signed in.
    """

    root = directory.resolve()
    if not (root / INDEX).is_file():
        raise ConfigurationError(
            f"LUDARIUM_FRONTEND_DIR is {directory}, which has no {INDEX}; "
            "build it with `pnpm run build` or leave the setting unset"
        )
    not_found = app.router.default

    async def frontend(scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope["path"]
        # An API path nothing answered is the API's 404, never the page: a
        # client calling a route that does not exist should not get HTML back.
        if (
            scope["type"] != "http"
            or scope["method"] not in READS
            or path == "/api"
            or path.startswith("/api/")
        ):
            await not_found(scope, receive, send)
            return
        file = (root / path.lstrip("/")).resolve()
        if file.is_relative_to(root) and file.is_file() and file != root / INDEX:
            cache = IMMUTABLE if file.parent == root / ASSETS else REVALIDATE
            response = FileResponse(file, headers={"Cache-Control": cache})
        else:
            # Any other path is a route of the single-page app, so a reload on
            # `/library?status=queued` gets the page that knows how to draw it.
            response = FileResponse(root / INDEX, headers={"Cache-Control": REVALIDATE})
        await response(scope, receive, send)

    app.router.default = frontend
