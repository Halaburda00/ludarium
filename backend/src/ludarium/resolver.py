"""Rule 9 with teeth: a provider writes provenance, only this writes columns.

`record()` and its batch form `record_many()` are the write path every provider
uses; `resolve()` decides which of the recorded assertions wins and puts it on
the entity. Keeping the two apart is
what turns rules 3 and 5 into a mechanism rather than a convention — the worst a
sync that goes wrong can do is add a row that loses.

Pinning, step 1 of the resolution order in `docs/schema.md`, is deliberately
absent: `field_pin` is not a table yet and arrives with the UI that writes it.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Final

from sqlalchemy import Enum as SqlEnum
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import EntityType, FieldStrategy, SourceKind
from ludarium.models import (
    Account,
    Base,
    Edition,
    Entitlement,
    EntitlementWork,
    FieldProvenance,
    Provider,
    UserWorkState,
    Work,
)
from ludarium.models.types import ScalarValue, utcnow
from ludarium.queries import in_batches, owned_by


class ResolutionError(Exception):
    """The resolver was asked for something the registry does not describe."""


class UnknownFieldError(ResolutionError):
    """A field nothing resolves. Almost always a typo or a missing registry entry."""


# `docs/schema.md`, in code. Keyed by table name, which is what `EntityType`
# holds for the four entities a provenance row can address. `user_work_state`
# is not one of them and never will be: its key is (user_id, work_id) and a
# provenance row carries a single `entity_id`, which is consistent with nothing
# sourcing those fields — they are aggregates, or the user's own.
STRATEGIES: Final[Mapping[tuple[str, str], FieldStrategy]] = {
    ("work", "title"): FieldStrategy.SINGLE_SOURCE,
    ("work", "sort_title"): FieldStrategy.PRECEDENCE,
    ("work", "summary"): FieldStrategy.PRECEDENCE,
    ("work", "release_year"): FieldStrategy.PRECEDENCE,
    ("work", "release_date"): FieldStrategy.PRECEDENCE,
    ("work", "item_kind"): FieldStrategy.PRECEDENCE,
    ("work", "metacritic_score"): FieldStrategy.SINGLE_SOURCE,
    ("work", "metacritic_url"): FieldStrategy.SINGLE_SOURCE,
    ("edition", "name"): FieldStrategy.PRECEDENCE,
    ("entitlement", "provider_title"): FieldStrategy.SINGLE_SOURCE,
    ("entitlement", "provider_item_id"): FieldStrategy.SINGLE_SOURCE,
    ("entitlement", "playtime_minutes"): FieldStrategy.MAX,
    ("entitlement", "installed"): FieldStrategy.AGENT_ONLY,
    ("entitlement", "install_path"): FieldStrategy.AGENT_ONLY,
    ("user_work_state", "playtime_minutes"): FieldStrategy.SUM,
    ("user_work_state", "last_played_at"): FieldStrategy.LATEST,
    ("user_work_state", "platform_count"): FieldStrategy.DERIVED,
    ("user_work_state", "play_status"): FieldStrategy.USER_ONLY,
    ("user_work_state", "rating"): FieldStrategy.USER_ONLY,
    ("user_work_state", "notes"): FieldStrategy.USER_ONLY,
    ("user_work_state", "is_favourite"): FieldStrategy.USER_ONLY,
}

ENTITIES: Final[Mapping[EntityType, type[Base]]] = {
    EntityType.WORK: Work,
    EntityType.EDITION: Edition,
    EntityType.ENTITLEMENT: Entitlement,
    EntityType.ACCOUNT: Account,
}

# Which source may write a field at all, where the answer is not "any of them".
# Rule 5's field-level exceptions, enforced at the write path rather than
# discovered later as a value that mysteriously never wins.
WRITERS: Final[Mapping[FieldStrategy, SourceKind]] = {
    FieldStrategy.AGENT_ONLY: SourceKind.LOCAL_AGENT,
    FieldStrategy.USER_ONLY: SourceKind.MANUAL,
}

# Honest about coverage: an untested branch that silently resolves something is
# worse than a refusal that says when it will exist. `record()` refuses these
# too — a write nothing can read back is not half-working, it is broken.
DEFERRED: Final[Mapping[FieldStrategy, str]] = {
    FieldStrategy.LATEST: "M4, where a second platform makes an aggregate mean something",
    FieldStrategy.DERIVED: "M4, with `platform_count` and the second platform",
    FieldStrategy.AGENT_ONLY: "the local agent, which the roadmap puts after M5",
}

# Registered, but not something `resolve()` can serve: these fields have no
# competing sources to choose between.
NOT_RESOLVED: Final[Mapping[FieldStrategy, str]] = {
    FieldStrategy.SUM: "aggregates entitlements; call resolve_work_aggregates()",
    FieldStrategy.USER_ONLY: "is written by the user, never recorded and resolved",
}

# Declaration order is the ladder of rule 5, highest first.
LADDER: Final[Mapping[SourceKind, int]] = {kind: rank for rank, kind in enumerate(SourceKind)}

# A provenance row may outlive the provider row its `source_ref` names, and the
# ladder decides nearly everything anyway; the weight only breaks ties inside
# one `SourceKind`. Matches the column default.
DEFAULT_WEIGHT: Final = 100
ACCOUNT_PREFIX: Final = "account:"

type Picker = Callable[[Sequence[FieldProvenance]], FieldProvenance | None]


def strategy_for(table: str, field: str) -> FieldStrategy:
    key = (str(table), field)
    if key not in STRATEGIES:
        raise UnknownFieldError(
            f"nothing resolves {key[0]}.{field}; the registry decides, not the caller"
        )
    return STRATEGIES[key]


def picker_for(strategy: FieldStrategy) -> Picker:
    """The function that decides, or a refusal naming what fills the gap in."""

    if strategy in DEFERRED:
        raise _deferral(strategy)
    if strategy in NOT_RESOLVED:
        raise ResolutionError(f"`{strategy.value}` {NOT_RESOLVED[strategy]}")
    return PICKERS[strategy]


def _deferral(strategy: FieldStrategy) -> NotImplementedError:
    return NotImplementedError(f"`{strategy.value}` arrives with {DEFERRED[strategy]}")


def _first(rows: Sequence[FieldProvenance]) -> FieldProvenance | None:
    """`precedence`: the rows arrive ordered, so the ladder has already spoken."""

    return rows[0] if rows else None


def _only(rows: Sequence[FieldProvenance]) -> FieldProvenance | None:
    """`single_source`: a second source means the field was misclassified, not that it ties."""

    if len(rows) > 1:
        sources = ", ".join(sorted(row.source_ref for row in rows))
        raise ResolutionError(f"single_source field asserted by several sources: {sources}")
    return rows[0] if rows else None


def _greatest(rows: Sequence[FieldProvenance]) -> FieldProvenance | None:
    """`max`: two sources describing the same play, where the lower figure is the stale one."""

    quantities = [(row, _quantity(row.value)) for row in rows]
    numeric = [(row, number) for row, number in quantities if number is not None]
    if not numeric:
        return None
    # `max` keeps the first of equals and the rows are ordered, so a tie falls
    # back to the ladder rather than to whatever the database returned first.
    return max(numeric, key=lambda pair: pair[1])[0]


def _claims_sole_source(strategy: FieldStrategy, source_kind: SourceKind) -> bool:
    """Whether this write is the one the `single_source` rule allows exactly one of.

    Two things depend on the answer and must never give different ones: the
    refusal below, and the flag `record()` writes for the partial unique index
    (ADR-0017). A runtime guard that excluded a source the flag still claimed
    for would leave the database enforcing a rule the code no longer holds.

    Manual is outside the rule rather than an exception to it — rule 3 puts a
    user override above the strategy, so it has to be able to sit beside the
    source it overrides.
    """

    return strategy is FieldStrategy.SINGLE_SOURCE and source_kind is not SourceKind.MANUAL


def _check_sole_source(
    rows: Sequence[FieldProvenance],
    strategy: FieldStrategy,
    entity_type: EntityType,
    field: str,
    source_kind: SourceKind,
    source_ref: str,
) -> None:
    """`single_source` at the write path, where the offending provider is still in hand.

    Left to `resolve()` the same check is conditional on there being no manual
    row: an override wins before any strategy runs, so a user editing the field
    would hide a provider writing where it must not. Refusing the write cannot
    be masked, and names who did it.

    Still check-then-act, and rule 4 makes the contention legal: two provider
    runs can both read the field as unclaimed. What stops them landing together
    is not this function but the transaction around it — and, where that is not
    enough, the `sole_source` index the insert below writes for (ADR-0017).
    """

    if not _claims_sole_source(strategy, source_kind):
        return
    rival = next(
        (
            row
            for row in rows
            if row.source_kind is not SourceKind.MANUAL and row.source_ref != source_ref
        ),
        None,
    )
    if rival is not None:
        raise ResolutionError(
            f"{entity_type.value}.{field} is single_source; {rival.source_ref} already asserts it"
        )


def _quantity(value: ScalarValue | None) -> float | None:
    # `bool` is an `int` in Python and a flag in the data; never a quantity.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


PICKERS: Final[Mapping[FieldStrategy, Picker]] = {
    FieldStrategy.PRECEDENCE: _first,
    FieldStrategy.SINGLE_SOURCE: _only,
    FieldStrategy.MAX: _greatest,
}


async def record_many(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity_id: int,
    source_kind: SourceKind,
    source_ref: str,
    values: Mapping[str, ScalarValue | None],
    run_id: int | None = None,
) -> dict[str, list[FieldProvenance]]:
    """Everything one source asserts about one entity, in one read and one flush.

    A provider states several fields about the same entitlement in the same
    breath, and the single-field form asked the same question once per field —
    same entity, same table, only `field` different. Three round-trips where the
    answer to all three arrives in one (#23).

    Returns every row for each field, rivals included, which is exactly what
    `resolve()` would otherwise read back a statement later.
    """

    strategies = {field: strategy_for(entity_type, field) for field in values}
    for field, strategy in strategies.items():
        writer = WRITERS.get(strategy)
        if writer is not None and source_kind is not writer:
            raise ResolutionError(
                f"{entity_type.value}.{field} is {strategy.value}; "
                f"{source_kind.value} may not write it"
            )
        if strategy in DEFERRED:
            raise _deferral(strategy)

    # Every row for every field, not just this source's: one query answers both
    # "is there a row to update" and "is someone else already asserting this".
    rows_by_field: dict[str, list[FieldProvenance]] = {field: [] for field in values}
    for existing in await session.scalars(
        select(FieldProvenance).where(
            FieldProvenance.entity_type == entity_type,
            FieldProvenance.entity_id == entity_id,
            FieldProvenance.field.in_(rows_by_field),
        )
    ):
        rows_by_field[existing.field].append(existing)

    # One timestamp for the batch: these fields did arrive in one answer, and
    # `_ordered` breaks a tie by it — so giving them separate moments would let
    # the order the caller happened to pass them in decide something.
    moment = utcnow()
    for field, value in values.items():
        rows = rows_by_field[field]
        _check_sole_source(rows, strategies[field], entity_type, field, source_kind, source_ref)
        row = next(
            (
                candidate
                for candidate in rows
                if candidate.source_kind is source_kind and candidate.source_ref == source_ref
            ),
            None,
        )
        if row is None:
            # The 5-tuple constraint covers one source writing twice; the
            # `sole_source` flag is what covers two sources sharing a field the
            # registry says only one may assert. Written from `STRATEGIES`
            # rather than named in the migration, so the two cannot drift
            # (ADR-0017).
            row = FieldProvenance(
                entity_type=entity_type,
                entity_id=entity_id,
                field=field,
                source_kind=source_kind,
                source_ref=source_ref,
                sole_source=_claims_sole_source(strategies[field], source_kind),
            )
            session.add(row)
            rows.append(row)
        row.value = value
        row.observed_at = moment
        row.run_id = run_id
    await session.flush()
    return rows_by_field


async def record(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity_id: int,
    field: str,
    source_kind: SourceKind,
    source_ref: str,
    value: ScalarValue | None,
    run_id: int | None = None,
) -> FieldProvenance:
    """One row per source per field, updated in place as the source changes its mind.

    A source can only ever touch its own row, which is why a later `platform_api`
    sync cannot reach a `manual` value (rule 3): it writes somewhere else.
    """

    rows = await record_many(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        source_kind=source_kind,
        source_ref=source_ref,
        values={field: value},
        run_id=run_id,
    )
    return next(
        row
        for row in rows[field]
        if row.source_kind is source_kind and row.source_ref == source_ref
    )


async def resolve(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity_id: int,
    fields: Sequence[str],
    recorded: Mapping[str, Sequence[FieldProvenance]] | None = None,
) -> dict[str, ScalarValue | None]:
    """Decide each field and write the winner onto the entity. Returns what was written.

    A field nobody asserts is left alone — a stub's title is set at creation and
    has no provenance row. A field whose sources have all withdrawn their value
    is not the same thing: the column is a cache of this decision, so it goes
    null rather than keeping a figure no row stands behind any more.

    `recorded` is what `record_many()` just read and wrote, offered so the same
    rows are not read a second statement later (#23). It must be every row for
    every named field, rivals included: resolving on a subset would decide by
    the ladder over half the sources, which is rule 6's false positive with no
    review queue to catch it.

    Naming a different set of fields than it resolves is refused below, which is
    the half of that contract a caller can get wrong by accident — subsetting
    what `record_many()` returned, or reusing a map from an earlier call.
    Nothing here can tell a complete field's rows from a pruned one's without
    the query the parameter exists to avoid, so the rule stays: only the write
    path that just built the map may pass it.

    Does not commit: the caller owns the transaction, and the whole point of the
    ordering below is that it is atomic.
    """

    if recorded is not None and set(recorded) != set(fields):
        raise ResolutionError(
            "`recorded` must name the fields being resolved and no others; "
            f"got {sorted(recorded)} for {sorted(fields)}"
        )

    entity = await session.get(ENTITIES[entity_type], entity_id)
    if entity is None:
        raise ResolutionError(f"no {entity_type.value} with id {entity_id}")

    strategies = {field: strategy_for(entity_type, field) for field in fields}
    rows_by_field = (
        {field: list(recorded[field]) for field in fields}
        if recorded is not None
        else await _rows_for(session, entity_type, entity_id, fields)
    )

    # One weights query for every source in play: a resolve after a sync asks
    # about several fields at once, and they name the same handful of providers.
    contested = [row for rows in rows_by_field.values() if len(rows) > 1 for row in rows]
    weights = await _weights(session, contested) if contested else {}

    written: dict[str, ScalarValue | None] = {}
    winners: list[FieldProvenance] = []
    for field, rows in rows_by_field.items():
        if not rows:
            continue

        ordered = _ordered(rows, weights)
        # The user override comes before the strategy (rule 3), so no strategy
        # has to remember to respect it.
        manual = [row for row in ordered if row.source_kind is SourceKind.MANUAL]
        winner = manual[0] if manual else picker_for(strategies[field])(_without_manual(ordered))
        value = winner.value if winner is not None else None

        setattr(entity, field, _as_column_type(entity, field, value))
        for row in rows:
            if row.is_effective and row is not winner:
                row.is_effective = False
        if winner is not None:
            winners.append(winner)
        written[field] = value

    # Columns and every cleared winner, then every new one — in that order, and
    # for all the fields at once rather than a pair of statements each. The
    # partial unique index rejects a moment with two winners for one field,
    # which is what it is for; a single flush could not be told to write the
    # clear before the set, but two flushes can. Fields do not interfere here —
    # the index is per field, so what has to be ordered is each field against
    # itself.
    await session.flush()
    await _mark_effective(session, winners)
    return written


async def _rows_for(
    session: AsyncSession, entity_type: EntityType, entity_id: int, fields: Sequence[str]
) -> dict[str, list[FieldProvenance]]:
    rows_by_field: dict[str, list[FieldProvenance]] = {field: [] for field in fields}
    for row in await session.scalars(
        select(FieldProvenance).where(
            FieldProvenance.entity_type == entity_type,
            FieldProvenance.entity_id == entity_id,
            FieldProvenance.field.in_(rows_by_field),
        )
    ):
        rows_by_field[row.field].append(row)
    return rows_by_field


async def resolve_work_aggregates_many(
    session: AsyncSession, *, work_ids: Sequence[int], user_id: int
) -> list[UserWorkState]:
    """`sum` across the entitlements of every work at once (rule 5).

    Two accounts are two stretches of play, not two reports of one, and a
    removed entitlement stops counting until it is restored (rule 1).

    Two queries for the whole list rather than two per work: a first sync
    touches one work per game, and asking about each of them separately was two
    round-trips per item of the library (#23).

    The other two aggregates in the registry stay unimplemented on purpose:
    `latest` and `derived` are both constants while there is one Steam account,
    and a strategy tested against a constant proves nothing.
    """

    wanted = list(dict.fromkeys(work_ids))
    if not wanted:
        return []

    states: dict[int, UserWorkState] = {}
    for batch in in_batches(wanted):
        for state in await session.scalars(
            select(UserWorkState).where(
                UserWorkState.user_id == user_id, UserWorkState.work_id.in_(batch)
            )
        ):
            states[state.work_id] = state
    missing = next((work_id for work_id in wanted if work_id not in states), None)
    if missing is not None:
        raise ResolutionError(f"no user_work_state for user {user_id} and work {missing}")

    totals: dict[int, int] = {}
    for batch in in_batches(wanted):
        for work_id, total in await session.execute(
            select(
                EntitlementWork.work_id,
                func.coalesce(func.sum(Entitlement.playtime_minutes), 0),
            )
            .join(Entitlement, Entitlement.id == EntitlementWork.entitlement_id)
            .where(EntitlementWork.work_id.in_(batch), *owned_by(user_id))
            .group_by(EntitlementWork.work_id)
        ):
            totals[work_id] = total

    for work_id in wanted:
        # A work whose every copy has been removed produces no group at all,
        # which is zero rather than "leave the old total alone" — the singular
        # form got the same answer from `coalesce` over an empty sum (rule 1).
        states[work_id].playtime_minutes = totals.get(work_id, 0)
    await session.flush()
    return [states[work_id] for work_id in wanted]


async def resolve_work_aggregates(
    session: AsyncSession, *, work_id: int, user_id: int
) -> UserWorkState:
    """One work, which is what a caller holding a single id wants to write."""

    return (await resolve_work_aggregates_many(session, work_ids=[work_id], user_id=user_id))[0]


async def _mark_effective(session: AsyncSession, rows: Sequence[FieldProvenance]) -> None:
    """The second of the two statements `resolve()` is careful to keep apart."""

    for row in rows:
        row.is_effective = True
    await session.flush()


def _without_manual(rows: Sequence[FieldProvenance]) -> list[FieldProvenance]:
    return [row for row in rows if row.source_kind is not SourceKind.MANUAL]


def _as_column_type(entity: Base, field: str, value: ScalarValue | None) -> object:
    """An enum column takes its member, so reading the entity back is not a lie.

    Everything else round-trips as itself. A date arriving as an ISO string is
    M2's problem, when IGDB becomes the first source to write one.
    """

    column = entity.__table__.columns[field]
    if value is None or not isinstance(column.type, SqlEnum):
        return value
    member: object = column.type.python_type(value)
    return member


def _ordered(rows: Sequence[FieldProvenance], weights: Mapping[str, int]) -> list[FieldProvenance]:
    """Best first: the ladder, then the provider's weight, then the newest."""

    return sorted(
        rows,
        key=lambda row: (
            LADDER[row.source_kind],
            -weights.get(row.source_ref, DEFAULT_WEIGHT),
            -row.observed_at.timestamp(),
        ),
    )


async def _weights(session: AsyncSession, rows: Sequence[FieldProvenance]) -> dict[str, int]:
    """`precedence_weight` per `source_ref`, which is a provider key or `account:12`."""

    refs = {row.source_ref for row in rows}
    account_ids = {
        int(ref.removeprefix(ACCOUNT_PREFIX))
        for ref in refs
        if ref.startswith(ACCOUNT_PREFIX) and ref.removeprefix(ACCOUNT_PREFIX).isdigit()
    }
    weights = {
        key: weight
        for key, weight in await session.execute(
            select(Provider.key, Provider.precedence_weight).where(Provider.key.in_(refs))
        )
    }
    if account_ids:
        rows_by_account = await session.execute(
            select(Account.id, Provider.precedence_weight)
            .join(Provider, Provider.id == Account.provider_id)
            .where(Account.id.in_(account_ids))
        )
        weights |= {
            f"{ACCOUNT_PREFIX}{account_id}": weight for account_id, weight in rows_by_account
        }
    return weights
