"""A matched work's summary, release date and companies, asked of IGDB (#82).

Run in the `igdb` run after anchoring, over every work anchoring has matched:
the id is the only thing IGDB can be asked about. Summary and date are
provenance like any field (rule 9), so a user's own value outranks IGDB's
(rule 3). Companies are rows of their own, linked to the work with IGDB named as
the source, so a later run replaces IGDB's links and nobody else's.

IGDB's data may not be redistributed. It lives in the fetch cache and in these
rows, all of them inside the data directory.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enrichment import EnrichmentRun, Step
from ludarium.enums import CompanyRole, EntityType
from ludarium.models import Company, ExternalId, Provider, WorkCompany
from ludarium.models.cache import Payload
from ludarium.models.types import ScalarValue
from ludarium.providers.base import MalformedResponseError, whole_number
from ludarium.providers.igdb import IgdbClient
from ludarium.resolver import record_many, resolve

logger = logging.getLogger(__name__)

NAMESPACE: Final = "igdb"
ENDPOINT: Final = "games"
# Cached apart from anchoring's `games`, which keeps names only: the two are
# asked different questions about the same ids.
RESOURCE: Final = "games/details"
# One row per id at most, so a batch this size cannot be cut short by IGDB's
# row limit.
BATCH: Final = 500
# Summaries are edited and release dates corrected, but slowly.
MAX_AGE: Final = timedelta(days=30)

# IGDB's flag on an involved company, and the role it means here.
ROLES: Final[Mapping[str, CompanyRole]] = {
    "developer": CompanyRole.DEVELOPER,
    "publisher": CompanyRole.PUBLISHER,
    "porting": CompanyRole.PORTING,
    "supporting": CompanyRole.SUPPORT,
}
FIELDS: Final = ",".join(
    [
        "summary",
        "first_release_date",
        "involved_companies.company.name",
        *(f"involved_companies.{flag}" for flag in ROLES),
    ]
)


def describe_matched_works(igdb: IgdbClient) -> Step:
    """The details half of the `igdb` step, run after anchoring."""

    async def details(games: Sequence[str]) -> dict[str, Payload]:
        rows = await igdb.query(
            ENDPOINT, f"fields {FIELDS}; where id = ({','.join(games)}); limit {BATCH};"
        )
        found: dict[str, Payload] = {}
        for row in rows:
            game = whole_number(row.get("id"))
            if game is None:
                raise MalformedResponseError(f"igdb returned a game without an id: {row}")
            found[str(game)] = _described(row)
        return found

    async def step(run: EnrichmentRun) -> None:
        games = await _anchored(run.database)
        answers = await run.fetch(
            RESOURCE,
            sorted({str(game) for game in games.values()}, key=int),
            fetch=details,
            batch_size=BATCH,
            max_age=MAX_AGE,
        )
        described = await _record(run, games, answers)
        logger.info("%d of %d matched works described by IGDB", described, len(games))

    return step


def _described(row: Mapping[str, Any]) -> Payload:
    """What is kept of a game: only what the step reads, as ADR-0023 keeps RAWG's."""

    summary = row.get("summary")
    released = whole_number(row.get("first_release_date"))
    companies: list[Payload] = []
    involved = row.get("involved_companies")
    for entry in involved if isinstance(involved, list) else []:
        company = entry.get("company") if isinstance(entry, dict) else None
        if not isinstance(company, dict):
            continue
        company_id, name = whole_number(company.get("id")), company.get("name")
        if company_id is None or not isinstance(name, str) or not name.strip():
            continue
        roles = [flag for flag in ROLES if entry.get(flag) is True]
        if roles:
            companies.append({"id": company_id, "name": name.strip(), "roles": roles})
    return {
        "summary": summary.strip() if isinstance(summary, str) and summary.strip() else None,
        "first_release_date": released,
        "companies": companies,
    }


def released_on(timestamp: int | None) -> datetime | None:
    """IGDB's `first_release_date`, seconds since the epoch, as a UTC moment.

    In UTC, not in the server's zone: a game released at midnight UTC on the
    first of January is that year's, wherever the NAS happens to be.
    """

    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


async def _anchored(database: Database) -> dict[int, int]:
    """Every anchored work and its IGDB game, from the authoritative `external_id` row."""

    async with database.reading_session_factory() as session:
        rows = await session.execute(
            select(ExternalId.entity_id, ExternalId.value).where(
                ExternalId.entity_type == EntityType.WORK,
                ExternalId.namespace == NAMESPACE,
            )
        )
        return {work_id: int(game) for work_id, game in rows if game.isdigit()}


async def _record(
    run: EnrichmentRun, games: Mapping[int, int], answers: Mapping[str, Payload | None]
) -> int:
    """Each work's summary, date and companies, in one transaction.

    Only what IGDB states is recorded. A missing summary asserts nothing rather
    than null, as the store's unknown kind does (ADR-0020): IGDB having nothing
    to say is not evidence that another source is wrong.
    """

    described = 0
    async with run.database.writing_session_factory() as session:
        reporter = await session.get_one(Provider, run.provider_id)
        for work_id, game in games.items():
            answer = answers.get(str(game))
            if not isinstance(answer, dict):
                continue
            described += 1
            values: dict[str, ScalarValue | None] = {}
            summary = answer.get("summary")
            if isinstance(summary, str):
                values["summary"] = summary
            moment = released_on(whole_number(answer.get("first_release_date")))
            if moment is not None:
                values["release_date"] = moment.date().isoformat()
                values["release_year"] = moment.year
            if values:
                recorded = await record_many(
                    session,
                    entity_type=EntityType.WORK,
                    entity_id=work_id,
                    source_kind=reporter.source_kind,
                    source_ref=reporter.key,
                    values=values,
                    run_id=run.id,
                )
                await resolve(
                    session,
                    entity_type=EntityType.WORK,
                    entity_id=work_id,
                    fields=list(values),
                    recorded=recorded,
                )
            await _link(session, reporter.key, work_id, answer.get("companies"))
        await session.commit()
    return described


async def _link(session: AsyncSession, source: str, work_id: int, companies: object) -> None:
    """Replace the work's links from `source` with the ones IGDB gives now.

    Replaced rather than added to: a publisher IGDB has since corrected would
    otherwise stay credited for good. Links from any other source stay.
    """

    wanted: set[tuple[int, CompanyRole]] = set()
    for entry in companies if isinstance(companies, list) else []:
        if not isinstance(entry, dict):
            continue
        igdb_id, name = whole_number(entry.get("id")), entry.get("name")
        if igdb_id is None or not isinstance(name, str):
            continue
        company = await _company(session, igdb_id, name)
        roles = entry.get("roles")
        for flag in roles if isinstance(roles, list) else []:
            if flag in ROLES:
                wanted.add((company.id, ROLES[flag]))

    await session.execute(
        delete(WorkCompany).where(WorkCompany.work_id == work_id, WorkCompany.source_ref == source)
    )
    held = {
        (company_id, role)
        for company_id, role in await session.execute(
            select(WorkCompany.company_id, WorkCompany.role).where(WorkCompany.work_id == work_id)
        )
    }
    for company_id, role in sorted(wanted - held):
        session.add(
            WorkCompany(work_id=work_id, company_id=company_id, role=role, source_ref=source)
        )
    await session.flush()


async def _company(session: AsyncSession, igdb_id: int, name: str) -> Company:
    """The company IGDB calls `igdb_id`, made if new and renamed if IGDB renamed it."""

    company = await session.scalar(select(Company).where(Company.igdb_id == igdb_id))
    if company is None:
        company = Company(igdb_id=igdb_id, name=name)
        session.add(company)
        await session.flush()
    elif company.name != name:
        company.name = name
    return company
