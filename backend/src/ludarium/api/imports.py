"""`/api/import`: a library from a CSV or JSON file, previewed and then applied (#130, ADR-0039).

Two requests carrying the same file rather than an upload kept between them:
nothing is stored that the user did not confirm, and there is no half-made
import to expire.
"""

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.api.sync import SyncRunResponse, describe_run
from ludarium.auth import CurrentSession
from ludarium.db import SessionDep
from ludarium.enums import EntitlementOrigin, SyncTrigger
from ludarium.imports import MAX_BYTES, Group, ParsedFile, UnreadableFileError, group, parse
from ludarium.ingest import (
    ConnectedAccountError,
    InactiveAccountError,
    IngestAccount,
    IngestItem,
    IngestReport,
    ingest,
)
from ludarium.models import Account, Entitlement, Provider
from ludarium.queries import owned_by
from ludarium.sync import SyncInProgressError

PREVIEW_PATH: Final = "/api/import/preview"
PATH: Final = "/api/import"
# What `main` bounds the request at: the file, and room for the multipart around it.
UPLOAD_BYTES: Final = MAX_BYTES + 64 * 1024
# The user typing a list in is the `manual` rung of rule 5, wherever the list
# came from (`docs/schema.md`).
REPORTER: Final = "manual"
# Enough to see that the file was read as meant: accents intact, the right
# column taken for the title.
SAMPLE: Final = 10
MAX_PROBLEMS: Final = 100

router = APIRouter(prefix="/import", tags=["import"])

type GroupStatus = Literal["new", "existing", "connected", "switched_off"]


class ImportProblem(BaseModel):
    row: int
    message: str


class ImportSample(BaseModel):
    row: int
    title: str
    platform: str


class ImportGroup(BaseModel):
    """One account the file fills, and what importing into it would do."""

    provider: str
    provider_name: str
    label: str
    items: int
    # `connected`: the platform syncs on its own here, so these rows are
    # skipped. `switched_off`: the account exists and takes nothing (#129).
    status: GroupStatus
    account_id: int | None
    # Copies the account holds that the file does not list: what a removal
    # sweep would mark removed. Zero for a new account.
    would_remove: int


class ImportPreview(BaseModel):
    format: str
    encoding: str | None
    delimiter: str | None
    columns: list[str]
    ignored_columns: list[str]
    rows_read: int
    # The first `MAX_PROBLEMS`; `problem_count` is all of them.
    problems: list[ImportProblem]
    problem_count: int
    sample: list[ImportSample]
    groups: list[ImportGroup]
    # Only a file read whole may remove: an unread row is a copy that would
    # otherwise be swept for being missing (rule 1).
    can_sweep: bool


class ImportOutcome(BaseModel):
    provider: str
    label: str
    status: GroupStatus
    # The run, for a group that was imported; null for one that was skipped or
    # refused, with `detail` saying why.
    run: SyncRunResponse | None
    detail: str | None


class ImportResult(BaseModel):
    outcomes: list[ImportOutcome]


@router.post("/preview")
async def preview(file: UploadFile, session: SessionDep, record: CurrentSession) -> ImportPreview:
    """What the file holds and where it would go. Writes nothing."""

    parsed, groups = await _read(file)
    planned = await _plan(session, groups, user_id=record.user_id)
    return ImportPreview(
        format=parsed.format,
        encoding=parsed.encoding,
        delimiter=parsed.delimiter,
        columns=parsed.columns,
        ignored_columns=parsed.ignored,
        rows_read=sum(len(each.items) for each in groups),
        problems=[
            ImportProblem(row=problem.row, message=problem.message)
            for problem in sorted(parsed.problems, key=lambda problem: problem.row)[:MAX_PROBLEMS]
        ],
        problem_count=len(parsed.problems),
        sample=[
            ImportSample(row=row.row, title=row.title, platform=row.platform)
            for row in parsed.rows[:SAMPLE]
        ],
        groups=[plan for plan, _ in planned],
        can_sweep=not parsed.problems,
    )


@router.post("")
async def apply(
    file: UploadFile,
    session: SessionDep,
    record: CurrentSession,
    sweep: Annotated[bool, Form()] = False,
) -> ImportResult:
    """Import every group the preview offered, one run each.

    A group failing does not stop the others, as one platform's outage does not
    stop another's sync (rule 4). `sweep` marks removed what an account holds
    and the file does not list, and is refused for a file with unread rows.
    """

    parsed, groups = await _read(file)
    if sweep and parsed.problems:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "a file with rows that could not be read cannot remove anything",
        )
    outcomes = []
    for plan, rows in await _plan(session, groups, user_id=record.user_id):
        outcome = ImportOutcome(
            provider=plan.provider, label=plan.label, status=plan.status, run=None, detail=None
        )
        outcomes.append(outcome)
        if plan.status in ("connected", "switched_off"):
            continue
        report = IngestReport(
            version=1,
            reporter=REPORTER,
            account=IngestAccount(
                provider=rows.provider,
                external_account_id=rows.external_account_id,
                label=rows.label,
            ),
            complete=sweep,
            items=[
                IngestItem(
                    provider_item_id=key,
                    title=row.title,
                    ownership_type=row.ownership_type,
                    item_kind=row.item_kind,
                    playtime_minutes=row.playtime_minutes,
                    acquired_at=row.acquired_at,
                    release_year=row.release_year,
                    # Where the copy came from, for whoever debugs it: never the
                    # file's other columns, which may hold anything (rule 7).
                    raw={"row": row.row},
                )
                for key, row in rows.items
            ],
        )
        try:
            finished = await ingest(
                session, report, user_id=record.user_id, trigger=SyncTrigger.IMPORT
            )
        except (ConnectedAccountError, InactiveAccountError, SyncInProgressError) as exc:
            outcome.detail = str(exc)
            continue
        outcome.run = describe_run(finished, REPORTER)
    return ImportResult(outcomes=outcomes)


async def _read(file: UploadFile) -> tuple[ParsedFile, list[Group]]:
    # One byte past the bound, so a file of exactly the bound is not refused.
    data = await file.read(MAX_BYTES + 1)
    try:
        parsed = parse(data, file.filename or "")
    except UnreadableFileError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    groups = group(parsed)
    return parsed, groups


async def _plan(
    session: AsyncSession, groups: list[Group], *, user_id: int
) -> list[tuple[ImportGroup, Group]]:
    """Each group with the account it lands in and what landing there would do."""

    providers = {
        provider.key: provider
        for provider in await session.scalars(
            select(Provider).where(Provider.key.in_({each.provider for each in groups}))
        )
    }
    # A platform the user connected syncs itself, and its sync is the better
    # source; a row about it is skipped rather than kept as a second copy.
    connected = set(
        await session.scalars(
            select(Provider.key)
            .join(Account, Account.provider_id == Provider.id)
            .where(Account.user_id == user_id, Account.is_derived.is_(False))
        )
    )
    planned = []
    for each in groups:
        provider = providers[each.provider]
        account = await session.scalar(
            select(Account).where(
                Account.provider_id == provider.id,
                Account.external_account_id == each.external_account_id,
                Account.user_id == user_id,
            )
        )
        state: GroupStatus
        if provider.key in connected:
            state = "connected"
        elif account is None:
            state = "new"
        elif not account.is_active:
            state = "switched_off"
        else:
            state = "existing"
        would_remove = 0
        if account is not None and state == "existing":
            listed = {key for key, _ in each.items}
            held = await session.scalars(
                select(Entitlement.provider_item_id).where(
                    Entitlement.account_id == account.id,
                    Entitlement.origin != EntitlementOrigin.MANUAL,
                    *owned_by(user_id),
                    # Kept by the user against a source's silence (ADR-0030).
                    Entitlement.kept_at.is_(None),
                )
            )
            would_remove = sum(1 for key in held if key not in listed)
        planned.append(
            (
                ImportGroup(
                    provider=provider.key,
                    provider_name=provider.display_name,
                    label=account.label if account is not None else each.label,
                    items=len(each.items),
                    status=state,
                    account_id=account.id if account is not None else None,
                    would_remove=would_remove,
                ),
                each,
            )
        )
    await session.commit()
    return planned
