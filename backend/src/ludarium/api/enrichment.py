"""Running an enrichment step by hand.

For the retry ADR-0019 names: a store outage ends a run that someone will want
to repeat without syncing the library again.
"""

import httpx
from fastapi import APIRouter, HTTPException, Request, status

from ludarium.api.common import provider_or_404
from ludarium.api.sync import SyncRunResponse, describe_run
from ludarium.auth import CurrentSession
from ludarium.db import Database, SessionDep
from ludarium.enrichment import EnrichmentInProgressError, enrich
from ludarium.steps import STEPS

router = APIRouter(prefix="/enrichment", tags=["enrichment"])


@router.post("/{provider}")
async def run(
    provider: str, request: Request, session: SessionDep, record: CurrentSession
) -> SyncRunResponse:
    """Run the provider's step and report the run, which may have failed.

    A failure is a status, as a sync's is, so the answer is 200 either way. 409
    is kept for the one thing the caller did: asking while a run is open.
    """

    reporter = await provider_or_404(session, provider)
    builder = STEPS.get(reporter.key)
    if builder is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"`{provider}` has no enrichment step")
    key = reporter.key
    # A POST's session began `IMMEDIATE`, and the lookups above are all it was
    # for. `enrich` writes in sessions of its own, which would otherwise wait
    # out `busy_timeout` behind this one and fail.
    await session.rollback()

    database: Database = request.app.state.database
    client: httpx.AsyncClient = request.app.state.http
    try:
        finished = await enrich(database, provider=key, step=builder(client))
    except EnrichmentInProgressError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return describe_run(finished, key)
