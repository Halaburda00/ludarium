"""Matching layer 1: a store's own id, looked up in IGDB `external_games` (#47).

A Steam appid or a GOG product id is looked up directly; an Epic game first
needs its store offers (#74).

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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from typing import Final

from ludamatch import ExternalId as StoreId
from ludamatch import MalformedRowError, Store, match_by_external_id, normalise_title
from ludamatch.igdb import BATCH_SIZE, SOURCE_IDS
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enrichment import BatchFetch, EnrichmentRun, Step
from ludarium.enums import EntityType, ItemKind, MatchActor, MatchLayer, WorkLinkRole
from ludarium.merging import MergeConflictError, collect_orphan_stubs, merge_work
from ludarium.models import Account, Entitlement, EntitlementWork, ExternalId, Provider, Work
from ludarium.models.cache import Payload
from ludarium.providers.base import MalformedResponseError, whole_number
from ludarium.providers.epic import NAMESPACE as NAMESPACE_ID
from ludarium.providers.epic import EpicCatalog
from ludarium.providers.igdb import IgdbClient
from ludarium.queries import in_batches
from ludarium.resolver import record_many, resolve
from ludarium.titles import sort_title

logger = logging.getLogger(__name__)

LIBRARY: Final = "steam"
EPIC: Final = "epic"
NAMESPACE: Final = "igdb"
EXTERNAL_GAMES: Final = "external_games/steam"
EXTERNAL_GAMES_EPIC: Final = "external_games/epic"
GOG: Final = "gog"
EXTERNAL_GAMES_GOG: Final = "external_games/gog"
EXTERNAL_GAMES_ENDPOINT: Final = "external_games"
# Which Steam appids IGDB files each game under, for works owned only elsewhere.
STEAM_APPIDS: Final = "external_games/steam_by_game"
STEAM_NAMESPACE: Final = "steam"
# IGDB's `external_game_sources` id for Steam, as `ludamatch` reads it.
STEAM_SOURCE: Final = SOURCE_IDS[Store.STEAM]
OFFERS: Final = "epic/offers"
BASE_GAME: Final = "BASE_GAME"
GAMES: Final = "games"

# IGDB revises its mappings, and an appid it did not know last month may be
# known now. A month matches the store's classification, and a re-ask costs one
# request per 500 appids.
MAX_AGE: Final = timedelta(days=30)


def anchor_steam_works(igdb: IgdbClient) -> Step:
    """The enrichment step, run under the `igdb` provider."""

    return _by_store_id(igdb, store=Store.STEAM, library=LIBRARY, cache=EXTERNAL_GAMES)


def anchor_gog_works(igdb: IgdbClient) -> Step:
    """Layer 1 for GOG (#128): a product id is a hard id in IGDB as an appid is.

    Measured on a 190-game library, IGDB named 179 of them. A GOG game has no
    Steam copy to give RAWG an appid to confirm a score by, so IGDB's appids are
    recorded afterwards, as for Epic.
    """

    by_product = _by_store_id(igdb, store=Store.GOG, library=GOG, cache=EXTERNAL_GAMES_GOG)

    async def step(run: EnrichmentRun) -> None:
        await by_product(run)
        await _record_steam_appids(run, igdb)

    return step


def _by_store_id(igdb: IgdbClient, *, store: Store, library: str, cache: str) -> Step:
    """Anchor a library's unmatched games by the store's own id for each."""

    async def external_games(appids: Sequence[str]) -> dict[str, Payload]:
        try:
            matches = await match_by_external_id(igdb, [StoreId(store, appid) for appid in appids])
        except MalformedRowError as exc:
            # A `ProviderError`, so the run fails as IGDB's rather than as a bug.
            raise MalformedResponseError(str(exc)) from exc
        return {key.uid: {"game": match.igdb_game_id} for key, match in matches.items()}

    games = _game_names(igdb)

    async def step(run: EnrichmentRun) -> None:
        targets = await _unmatched_games(run.database, library)
        anchors = await run.fetch(
            cache,
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
        await _anchor(run, targets, found, names, _vouch_through(library))
        await _collect_orphans(run.database)

    return step


def _game_names(igdb: IgdbClient) -> BatchFetch:
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

    return games


def _game(payload: Payload | None) -> int | None:
    return whole_number(payload.get("game")) if isinstance(payload, dict) else None


async def _unmatched_games(database: Database, library: str) -> list[tuple[str, int]]:
    """Each store id behind an unmatched game, with the work it reaches, oldest work first.

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
                Provider.key == library,
                Entitlement.provider_item_id.is_not(None),
                Work.is_matched.is_(False),
                Work.item_kind == ItemKind.GAME,
            )
            .order_by(Work.id, Entitlement.id)
        )
        # Not null by the predicate above; mypy cannot see that.
        return [(str(appid), work_id) for appid, work_id in rows]


type Vouch = Callable[[AsyncSession, int, str], Awaitable[None]]


async def _anchor(
    run: EnrichmentRun,
    targets: Sequence[tuple[str, int]],
    found: Mapping[str, int],
    names: Mapping[str, Payload | None],
    vouch: Vouch,
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
                    session, run.id, reporter, appid, work_id, game, names.get(str(game)), vouch
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
                await vouch(session, holder, appid)
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
    vouch: Vouch,
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
    await vouch(session, work_id, appid)

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


def _vouch_through(library: str) -> Vouch:
    """Mark the link that led to `work_id` with the layer that vouched for it.

    Only this store id's: another entitlement reaching the same work got there
    some other way, and saying otherwise would be an audit trail that lies.
    """

    async def vouch(session: AsyncSession, work_id: int, appid: str) -> None:
        await _vouch(session, work_id, appid, library)

    return vouch


async def _vouch(session: AsyncSession, work_id: int, appid: str, library: str) -> None:
    await session.execute(
        update(EntitlementWork)
        .where(
            EntitlementWork.work_id == work_id,
            EntitlementWork.role == WorkLinkRole.PRIMARY,
            EntitlementWork.entitlement_id.in_(
                select(Entitlement.id)
                .join(Account, Account.id == Entitlement.account_id)
                .join(Provider, Provider.id == Account.provider_id)
                .where(Provider.key == library, Entitlement.provider_item_id == appid)
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


def anchor_epic_works(igdb: IgdbClient, catalog: EpicCatalog) -> Step:
    """Layer 1 for Epic (#74): a work's namespace, its store offers, then IGDB.

    None of an Epic library record's ids is IGDB's `uid` for Epic, which is the
    store's offer id (measured, #64). A namespace's `BASE_GAME` offers are, and
    the catalogue lists them for the launcher's app token, so no user's sign-in
    is spent here. Measured on a 562-game library: exactly one IGDB game for 464.

    A namespace whose base offers name two IGDB games anchors nothing (rule 6).
    One holding several of the library's games — a game beside its beta, its
    test branch, its soundtrack — anchors only the work titled as the base
    offer is, compared through `ludamatch`'s normalisation. The rest stay
    stubs rather than being folded into the game they sit beside.
    """

    async def offers(namespaces: Sequence[str]) -> dict[str, Payload]:
        (namespace,) = namespaces
        base = [
            {"id": offer["id"], "title": offer.get("title")}
            for offer in await catalog.offers(namespace)
            if offer.get("offerType") == BASE_GAME
        ]
        return {namespace: {"base_games": base}}

    async def external_games(offer_ids: Sequence[str]) -> dict[str, Payload]:
        try:
            matches = await match_by_external_id(
                igdb, [StoreId(Store.EPIC, offer) for offer in offer_ids]
            )
        except MalformedRowError as exc:
            raise MalformedResponseError(str(exc)) from exc
        return {key.uid: {"game": match.igdb_game_id} for key, match in matches.items()}

    async def step(run: EnrichmentRun) -> None:
        targets = await _unmatched_epic_games(run.database)
        namespaces = sorted({namespace for namespace, _, _ in targets})
        answers = await run.fetch(OFFERS, namespaces, fetch=offers, batch_size=1, max_age=MAX_AGE)
        base = {namespace: _base_offers(answers.get(namespace)) for namespace in namespaces}
        anchors = await run.fetch(
            EXTERNAL_GAMES_EPIC,
            sorted({offer for offers_ in base.values() for offer, _ in offers_}),
            fetch=external_games,
            batch_size=BATCH_SIZE,
            max_age=MAX_AGE,
        )
        game_of = {
            offer: game
            for offer, payload in anchors.items()
            if (game := _game(payload)) is not None
        }
        pairs, found = _epic_anchors(targets, base, game_of)
        names = await run.fetch(
            GAMES,
            sorted({str(game) for game in found.values()}, key=int),
            fetch=_game_names(igdb),
            batch_size=BATCH_SIZE,
            max_age=MAX_AGE,
        )
        await _anchor(run, pairs, found, names, _vouch_epic)
        await _record_steam_appids(run, igdb)

    return step


def _base_offers(payload: Payload | None) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    return [
        (offer["id"], offer.get("title") or "")
        for offer in payload.get("base_games", [])
        if isinstance(offer, dict) and isinstance(offer.get("id"), str)
    ]


def _epic_anchors(
    targets: Sequence[tuple[str, int, str]],
    base: Mapping[str, Sequence[tuple[str, str]]],
    game_of: Mapping[str, int],
) -> tuple[list[tuple[str, int]], dict[str, int]]:
    """Which work each namespace anchors, and to which game. Keys are `namespace/work`."""

    works: dict[str, list[tuple[int, str]]] = {}
    for namespace, work_id, title in targets:
        works.setdefault(namespace, []).append((work_id, title))
    pairs: list[tuple[str, int]] = []
    found: dict[str, int] = {}
    for namespace, candidates in works.items():
        offers = [(offer, title) for offer, title in base.get(namespace, []) if offer in game_of]
        games = {game_of[offer] for offer, _ in offers}
        if len(games) != 1:
            continue
        (game,) = games
        # Every work, not only where a namespace holds several: a beta owned
        # without its game is the only game-kind item in its namespace, and is
        # still not the game (#77 review). Equal, or the start of the offer's
        # title — offers carry edition suffixes ("Watch Dogs 2 Standard
        # Edition") that the library's own titles leave off. Measured: keeps
        # 419 of 423 right anchors and turns the beta away.
        titled = [normalise_title(title) for _, title in offers]
        candidates = [
            (work_id, title)
            for work_id, title in candidates
            if _named_by(normalise_title(title), titled)
        ]
        if len(candidates) != 1:
            continue
        ((work_id, _),) = candidates
        key = f"{namespace}/{work_id}"
        pairs.append((key, work_id))
        found[key] = game
    # Oldest first, as for Steam: the older of two works naming one game is anchored.
    return sorted(pairs, key=lambda pair: pair[1]), found


def _named_by(title: str, offers: Sequence[str]) -> bool:
    """Whether an offer is titled as the work, give or take an edition after it."""

    return bool(title) and any(offer == title or offer.startswith(f"{title} ") for offer in offers)


async def _unmatched_epic_games(database: Database) -> list[tuple[str, int, str]]:
    """Each unmatched game an Epic entitlement reaches: its namespace, work and store title."""

    async with database.reading_session_factory() as session:
        rows = await session.execute(
            select(Entitlement.raw_payload, Entitlement.provider_title, Work.id)
            .join(
                EntitlementWork,
                (EntitlementWork.entitlement_id == Entitlement.id)
                & (EntitlementWork.role == WorkLinkRole.PRIMARY),
            )
            .join(Work, Work.id == EntitlementWork.work_id)
            .join(Account, Account.id == Entitlement.account_id)
            .join(Provider, Provider.id == Account.provider_id)
            .where(
                Provider.key == EPIC,
                Work.is_matched.is_(False),
                Work.item_kind == ItemKind.GAME,
            )
            .order_by(Work.id, Entitlement.id)
        )
        targets: dict[int, tuple[str, int, str]] = {}
        for raw, title, work_id in rows:
            namespace = raw.get("namespace") if isinstance(raw, dict) else None
            if isinstance(namespace, str) and NAMESPACE_ID.fullmatch(namespace):
                targets.setdefault(work_id, (namespace, work_id, title))
        return list(targets.values())


async def _vouch_epic(session: AsyncSession, work_id: int, key: str) -> None:
    """Mark the Epic links that led here: this namespace's, and no other entitlement's."""

    namespace = key.split("/", 1)[0]
    rows = await session.execute(
        select(Entitlement.id, Entitlement.raw_payload)
        .join(
            EntitlementWork,
            (EntitlementWork.entitlement_id == Entitlement.id)
            & (EntitlementWork.work_id == work_id)
            & (EntitlementWork.role == WorkLinkRole.PRIMARY),
        )
        .join(Account, Account.id == Entitlement.account_id)
        .join(Provider, Provider.id == Account.provider_id)
        .where(Provider.key == EPIC)
    )
    vouched = [
        entitlement_id
        for entitlement_id, raw in rows
        if isinstance(raw, dict) and raw.get("namespace") == namespace
    ]
    if vouched:
        await session.execute(
            update(EntitlementWork)
            .where(
                EntitlementWork.work_id == work_id,
                EntitlementWork.entitlement_id.in_(vouched),
            )
            .values(match_layer=MatchLayer.HARD_ID)
        )


async def _record_steam_appids(run: EnrichmentRun, igdb: IgdbClient) -> None:
    """IGDB's Steam appids for every anchored work no Steam copy gives one to.

    What RAWG confirms a Metacritic candidate by (#49): a game owned only on
    Epic has no Steam entitlement to read an appid from, and IGDB's own
    `external_games` row is a hard id as good as ours. Kept as an authoritative
    `external_id` of the work, so the score step reads both the same way.
    """

    wanted = await _without_steam_appid(run.database)
    if not wanted:
        return

    async def appids(games: Sequence[str]) -> dict[str, Payload]:
        rows: list[dict[str, object]] = []
        while True:
            page = await igdb.query(
                EXTERNAL_GAMES_ENDPOINT,
                f"fields game,uid; where external_game_source = {STEAM_SOURCE} "
                f"& game = ({','.join(games)}); sort id asc; limit {BATCH_SIZE}; "
                f"offset {len(rows)};",
            )
            rows += page
            if len(page) < BATCH_SIZE:
                break
        found: dict[str, list[str]] = {}
        for row in rows:
            game, uid = whole_number(row.get("game")), row.get("uid")
            if game is None or not isinstance(uid, str):
                raise MalformedResponseError(
                    f"igdb returned an external game it cannot name: {row}"
                )
            if uid.isdigit():
                found.setdefault(str(game), []).append(uid)
        return {game: {"appids": sorted(set(uids), key=int)} for game, uids in found.items()}

    answers = await run.fetch(
        STEAM_APPIDS,
        sorted({str(game) for game in wanted.values()}, key=int),
        fetch=appids,
        batch_size=BATCH_SIZE,
        max_age=MAX_AGE,
    )
    async with run.database.writing_session_factory() as session:
        reporter = await session.get_one(Provider, run.provider_id)
        taken = set(
            await session.scalars(
                select(ExternalId.value).where(
                    ExternalId.namespace == STEAM_NAMESPACE,
                    ExternalId.entity_type == EntityType.WORK,
                )
            )
        )
        for work_id, game in wanted.items():
            payload = answers.get(str(game))
            for appid in payload.get("appids", []) if isinstance(payload, dict) else []:
                if not isinstance(appid, str) or appid in taken:
                    continue
                session.add(
                    ExternalId(
                        entity_type=EntityType.WORK,
                        entity_id=work_id,
                        namespace=STEAM_NAMESPACE,
                        value=appid,
                        is_authoritative=True,
                        source_ref=reporter.key,
                    )
                )
                taken.add(appid)
        await session.commit()


async def _without_steam_appid(database: Database) -> dict[int, int]:
    """Anchored works with no Steam copy and no Steam appid yet, with their IGDB game."""

    async with database.reading_session_factory() as session:
        steam_owned = (
            select(EntitlementWork.work_id)
            .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
            .join(Account, Account.id == Entitlement.account_id)
            .join(Provider, Provider.id == Account.provider_id)
            .where(Provider.key == LIBRARY)
        )
        has_appid = select(ExternalId.entity_id).where(
            ExternalId.namespace == STEAM_NAMESPACE, ExternalId.entity_type == EntityType.WORK
        )
        rows = await session.execute(
            select(ExternalId.entity_id, ExternalId.value).where(
                ExternalId.namespace == NAMESPACE,
                ExternalId.entity_type == EntityType.WORK,
                ExternalId.entity_id.not_in(steam_owned),
                ExternalId.entity_id.not_in(has_appid),
            )
        )
        return {work_id: int(game) for work_id, game in rows if game.isdigit()}
