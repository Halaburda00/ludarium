"""Matching layer 1: a Steam appid, looked up in IGDB `external_games` (#47).

A hard id is not a similarity score, so this layer anchors a work, folds it
into the work already anchored to the same game (#48), or leaves it alone.
Where IGDB does not know the appid, or knows it under two games, the work stays
a stub for a later layer: a false positive costs more than a false negative
(rule 6). The query and the reading of its answer are `ludamatch`'s; this
module decides which works to ask about and what an answer does to them.

Only works known to be games are asked about. A playtest's appid resolves in
IGDB to the game it tests, and `item_kind` is null until #41's step has said
otherwise, so an unclassified work waits rather than being guessed at.

The anchor is an `external_id` row, and `work.igdb_id` and `work.is_matched`
are copies of it kept for lookups rather than resolved fields (ADR-0021). What
IGDB says the game is called is a provenance row like any other, so the
resolver still writes the title and a user's own still wins (rules 3 and 9).
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Final

from ludamatch import ExternalId as StoreId
from ludamatch import MalformedRowError, Store, match_by_external_id
from ludamatch.igdb import BATCH_SIZE
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enrichment import EnrichmentRun, Step
from ludarium.enums import EntityType, ItemKind, MatchActor, MatchLayer, WorkLinkRole
from ludarium.merging import MergeConflictError, collect_orphan_stubs, merge_work
from ludarium.models import Account, Entitlement, EntitlementWork, ExternalId, Provider, Work
from ludarium.models.cache import Payload
from ludarium.providers.base import MalformedResponseError, whole_number
from ludarium.providers.igdb import IgdbClient
from ludarium.queries import in_batches
from ludarium.resolver import record_many, resolve
from ludarium.titles import sort_title

logger = logging.getLogger(__name__)

LIBRARY: Final = "steam"
NAMESPACE: Final = "igdb"
EXTERNAL_GAMES: Final = "external_games/steam"
GAMES: Final = "games"

# IGDB revises its mappings, and an appid it did not know last month may be
# known now. A month matches the store's classification, and a re-ask costs one
# request per 500 appids.
MAX_AGE: Final = timedelta(days=30)


def anchor_steam_works(igdb: IgdbClient) -> Step:
    """The enrichment step, run under the `igdb` provider."""

    async def external_games(appids: Sequence[str]) -> dict[str, Payload]:
        try:
            matches = await match_by_external_id(
                igdb, [StoreId(Store.STEAM, appid) for appid in appids]
            )
        except MalformedRowError as exc:
            # A `ProviderError`, so the run fails as IGDB's rather than as a bug.
            raise MalformedResponseError(str(exc)) from exc
        return {key.uid: {"game": match.igdb_game_id} for key, match in matches.items()}

    async def games(ids: Sequence[str]) -> dict[str, Payload]:
        # One row per id at most, so a batch of `BATCH_SIZE` ids cannot be cut
        # short by the row limit the way `external_games` can.
        rows = await igdb.query(
            GAMES, f"fields name; where id = ({','.join(ids)}); limit {BATCH_SIZE};"
        )
        named: dict[str, Payload] = {}
        for row in rows:
            game, name = whole_number(row.get("id")), row.get("name")
            if game is None or not isinstance(name, str):
                raise MalformedResponseError(
                    f"igdb returned a game without an id and a name: {row}"
                )
            named[str(game)] = {"name": name}
        return named

    async def step(run: EnrichmentRun) -> None:
        targets = await _unmatched_games(run.database)
        anchors = await run.fetch(
            EXTERNAL_GAMES,
            [appid for appid, _ in targets],
            fetch=external_games,
            batch_size=BATCH_SIZE,
            max_age=MAX_AGE,
        )
        found = {
            appid: game
            for appid, payload in anchors.items()
            if (game := _game(payload)) is not None
        }
        names = await run.fetch(
            GAMES,
            sorted({str(game) for game in found.values()}, key=int),
            fetch=games,
            batch_size=BATCH_SIZE,
            max_age=MAX_AGE,
        )
        await _anchor(run, targets, found, names)
        await _collect_orphans(run.database)

    return step


def _game(payload: Payload | None) -> int | None:
    return whole_number(payload.get("game")) if isinstance(payload, dict) else None


async def _unmatched_games(database: Database) -> list[tuple[str, int]]:
    """Each Steam appid behind an unmatched game, with the work it reaches, oldest work first.

    Oldest first because two stubs can carry appids IGDB files under one game
    — a standard and a GOTY release — and `docs/schema.md` gives the anchor to
    the older row when neither is matched. Removed entitlements are included,
    as in classification: a game that left the account is still that game.
    """

    async with database.reading_session_factory() as session:
        rows = await session.execute(
            select(Entitlement.provider_item_id, Work.id)
            .join(
                EntitlementWork,
                (EntitlementWork.entitlement_id == Entitlement.id)
                & (EntitlementWork.role == WorkLinkRole.PRIMARY),
            )
            .join(Work, Work.id == EntitlementWork.work_id)
            .join(Account, Account.id == Entitlement.account_id)
            .join(Provider, Provider.id == Account.provider_id)
            .where(
                Provider.key == LIBRARY,
                Entitlement.provider_item_id.is_not(None),
                Work.is_matched.is_(False),
                Work.item_kind == ItemKind.GAME,
            )
            .order_by(Work.id, Entitlement.id)
        )
        # Not null by the predicate above; mypy cannot see that.
        return [(str(appid), work_id) for appid, work_id in rows]


async def _anchor(
    run: EnrichmentRun,
    targets: Sequence[tuple[str, int]],
    found: Mapping[str, int],
    names: Mapping[str, Payload | None],
) -> None:
    """Anchor each work IGDB named, and fold into it every other work naming the same game.

    One transaction. A game another work already holds — anchored by an earlier
    run, or a moment ago by this one — makes the second work a duplicate, and a
    hard id is the one match certain enough to merge on without asking (rule 6).
    The merge is audited and undoable all the same.

    A merge the registry refuses, two works each taking a `single_source` field
    from a different source, leaves both cards standing for review.
    """

    async with run.database.writing_session_factory() as session:
        reporter = await session.get_one(Provider, run.provider_id)
        wanted = sorted({str(game) for game in found.values()})
        holders: dict[str, int] = {}
        for batch in in_batches(wanted):
            holders |= {
                value: entity_id
                for value, entity_id in await session.execute(
                    select(ExternalId.value, ExternalId.entity_id).where(
                        ExternalId.namespace == NAMESPACE,
                        ExternalId.entity_type == EntityType.WORK,
                        ExternalId.value.in_(batch),
                    )
                )
            }

        # Works this run has settled: anchored, or folded into another.
        settled: set[int] = set()
        merged = conflicted = 0
        for appid, work_id in targets:
            game = found.get(appid)
            if game is None or work_id in settled:
                continue
            holder = holders.get(str(game))
            if holder is None:
                await _anchor_one(
                    session, run.id, reporter, appid, work_id, game, names.get(str(game))
                )
                holders[str(game)] = work_id
            else:
                try:
                    await merge_work(
                        session,
                        source_id=work_id,
                        target_id=holder,
                        layer=MatchLayer.HARD_ID,
                        actor=MatchActor.AUTO,
                    )
                except MergeConflictError as exc:
                    conflicted += 1
                    logger.warning("work %d is not merged into work %d: %s", work_id, holder, exc)
                    continue
                await _vouch(session, holder, appid)
                merged += 1
            settled.add(work_id)
        await session.commit()

    if merged:
        logger.info("%d works merged into the work that holds their IGDB game", merged)
    if conflicted:
        logger.info("%d works name a held IGDB game but conflict with its holder", conflicted)


async def _anchor_one(
    session: AsyncSession,
    run_id: int,
    reporter: Provider,
    appid: str,
    work_id: int,
    game: int,
    named: Payload | None,
) -> None:
    session.add(
        ExternalId(
            entity_type=EntityType.WORK,
            entity_id=work_id,
            namespace=NAMESPACE,
            value=str(game),
            is_authoritative=True,
            source_ref=reporter.key,
        )
    )
    work = await session.get_one(Work, work_id)
    work.igdb_id = game
    work.is_matched = True
    await _vouch(session, work_id, appid)

    name = named.get("name") if isinstance(named, dict) else None
    if not isinstance(name, str) or not name.strip():
        # Anchored all the same: the id is the match, the name only its label,
        # and the stub keeps the store's until IGDB gives one.
        return
    # `sort_title` too, derived from IGDB's name as the stub's was derived from
    # the store's. Otherwise the card would read the new title and file under
    # the old one; a user's own `sort_title` still outranks it (rule 3).
    fields = {"title": name, "sort_title": sort_title(name)}
    recorded = await record_many(
        session,
        entity_type=EntityType.WORK,
        entity_id=work_id,
        source_kind=reporter.source_kind,
        source_ref=reporter.key,
        values=fields,
        run_id=run_id,
    )
    await resolve(
        session,
        entity_type=EntityType.WORK,
        entity_id=work_id,
        fields=list(fields),
        recorded=recorded,
    )


async def _vouch(session: AsyncSession, work_id: int, appid: str) -> None:
    """Mark the link that led to `work_id` with the layer that vouched for it.

    Only this appid's: another entitlement reaching the same work got there some
    other way, and saying otherwise would be an audit trail that lies.
    """

    await session.execute(
        update(EntitlementWork)
        .where(
            EntitlementWork.work_id == work_id,
            EntitlementWork.role == WorkLinkRole.PRIMARY,
            EntitlementWork.entitlement_id.in_(
                select(Entitlement.id)
                .join(Account, Account.id == Entitlement.account_id)
                .join(Provider, Provider.id == Account.provider_id)
                .where(Provider.key == LIBRARY, Entitlement.provider_item_id == appid)
            ),
        )
        .values(match_layer=MatchLayer.HARD_ID)
    )


async def _collect_orphans(database: Database) -> None:
    """The orphan-stub job, here until M4 brings a scheduler to run it on its own.

    A merge deletes its own source, so this finds the stubs left unreached some
    other way. Its own transaction: the anchors stand whatever it finds.
    """

    async with database.writing_session_factory() as session:
        report = await collect_orphan_stubs(session)
        await session.commit()
    if report.deleted:
        logger.info("%d orphaned stubs deleted", len(report.deleted))
