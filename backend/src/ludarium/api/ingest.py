"""`POST /api/ingest`: a library reported by something that is not a platform client (ADR-0035)."""

from collections.abc import Mapping
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
    """Refuse a request past its path's bound, before FastAPI buffers it to validate.

    The declared length is checked first; a body sent without one, or longer
    than it said, is counted as it arrives. Only this path and those in
    `others`, the ones that take a library: everything else the API takes is a
    few fields.
    """

    def __init__(self, app: ASGIApp, others: Mapping[str, int] | None = None) -> None:
        self.app = app
        self.others = dict(others or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = None
        if scope["type"] == "http":
            # This path's bound read per request rather than captured, as it was
            # before `others` existed.
            limit = MAX_BYTES if scope["path"] == PATH else self.others.get(scope["path"])
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _too_large(limit)(scope, receive, send)
            return

        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised inside FastAPI's body read, which lets an
                    # HTTPException through as itself.
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, _too_large_text(limit))
            return message

        await self.app(scope, counted, send)


def _too_large_text(limit: int) -> str:
    return f"this request is at most {limit // (1024 * 1024)} MB"


def _too_large(limit: int) -> JSONResponse:
    return JSONResponse(
        {"detail": _too_large_text(limit)}, status_code=status.HTTP_413_CONTENT_TOO_LARGE
    )
