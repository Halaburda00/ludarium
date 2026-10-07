"""`POST /api/ingest`: a library reported by something that is not a platform client (ADR-0035)."""

from typing import Final

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ludarium.api.sync import SyncRunResponse, describe_run
from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.ingest import (
    ConnectedAccountError,
    InactiveAccountError,
    IngestRefusedError,
    IngestReport,
    ingest,
)
from ludarium.sync import SyncInProgressError

PATH: Final = "/api/ingest"
# The whole request, raw payloads included. Measured, a 5000-item report with
# a small `raw` per item is 1.2 MB, so this leaves room for the largest library
# and refuses before anything has been buffered past it.
MAX_BYTES: Final = 32 * 1024 * 1024

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("")
async def report(
    payload: IngestReport, session: SessionDep, record: CurrentSession
) -> SyncRunResponse:
    """Apply a report and answer with its run, which may have failed.

    A failure is a status, as a sync's is, so the answer is 200 either way. 409
    is for an account that is busy or connected, 422 for a report naming what
    cannot report or be reported on.
    """

    try:
        finished = await ingest(session, payload, user_id=record.user_id)
    except (ConnectedAccountError, InactiveAccountError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IngestRefusedError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except SyncInProgressError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return describe_run(finished, payload.reporter)


class BodyLimit:
    """Refuse an ingest request past `MAX_BYTES`, before FastAPI buffers it to validate.

    The declared length is checked first; a body sent without one, or longer
    than it said, is counted as it arrives. Only this path: everything else the
    API takes is a few fields.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != PATH:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > MAX_BYTES:
            await _too_large()(scope, receive, send)
            return

        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BYTES:
                    # Raised inside FastAPI's body read, which lets an
                    # HTTPException through as itself.
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, _TOO_LARGE)
            return message

        await self.app(scope, counted, send)


_TOO_LARGE: Final = f"an ingest report is at most {MAX_BYTES // (1024 * 1024)} MB"


def _too_large() -> JSONResponse:
    return JSONResponse({"detail": _TOO_LARGE}, status_code=status.HTTP_413_CONTENT_TOO_LARGE)
