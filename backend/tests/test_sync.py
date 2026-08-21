import ast
import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_provider, make_user
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import queries
from ludarium import sync as sync_module
from ludarium.db import Database
from ludarium.enums import (
    EntitlementOrigin,
    EntityType,
    ItemKind,
    OwnershipType,
    SourceKind,
    SyncStatus,
    SyncTrigger,
    WorkLinkRole,
)
from ludarium.models import (
    Account,
    Edition,
    Entitlement,
    EntitlementWork,
    FieldProvenance,
    UserWorkState,
    Work,
)
from ludarium.models.types import ScalarValue, utcnow
from ludarium.providers import (
    LibraryItem,
    ProviderUnavailableError,
    SteamCredentials,
    SteamProvider,
)
from ludarium.providers import steam as steam_module
from ludarium.resolver import STRATEGIES
from ludarium.sync import SyncError, sync_account

FIXTURES = Path(__file__).parent / "fixtures" / "steam"
OWNED_GAMES_URL = f"{steam_module.STEAM_API}{steam_module.OWNED_GAMES}"


class FakeLibrary:
    """A `LibraryProvider` whose answer the test dictates.

    The Steam client has its own suite; what matters here is what the sync
    service does with a library, not how one is fetched. One end-to-end test
    through `SteamProvider` and a recorded fixture keeps the two joined up.
    """

    def __init__(
        self,
        items: list[LibraryItem] | None = None,
        *,
        key: str = "steam",
        error: Exception | None = None,
    ) -> None:
        self.key = key
        self.calls = 0
        self._items = items or []
        self._error = error

    async def validate_credentials(self) -> None:
        return None

    async def fetch_library(self) -> list[LibraryItem]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._items)


def owned(
    provider_item_id: str,
    title: str,
    *,
    playtime_minutes: int | None = 0,
    **extra: Any,
) -> LibraryItem:
    return LibraryItem(
        provider_item_id=provider_item_id,
        title=title,
        playtime_minutes=playtime_minutes,
        **extra,
    )


THREE_GAMES = [
    owned("292030", "The Witcher 3: Wild Hunt", playtime_minutes=3247),
    owned("620", "Portal 2"),
    owned("570", "Dota 2", playtime_minutes=12),
]


@pytest.fixture
async def account(session: AsyncSession) -> Account:
    return await make_account(session)


async def count(session: AsyncSession, model: type[Any]) -> int:
    total = await session.scalar(select(func.count()).select_from(model))
    assert total is not None
    return total


async def one_entitlement(session: AsyncSession, provider_item_id: str) -> Entitlement:
    entitlement = await session.scalar(
        select(Entitlement).where(Entitlement.provider_item_id == provider_item_id)
    )
    assert entitlement is not None
    return entitlement


async def test_a_first_run_gives_every_entitlement_a_work(
    session: AsyncSession, account: Account
) -> None:
    """ADR-0015: the stub, its default edition and the primary link, all at once.

    The grid is work-centric from the first run, so there is no state in which
    an entitlement has no work and nothing to hang `user_work_state` off.
    """

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert await count(session, Entitlement) == 3
    assert await count(session, Work) == 3
    assert await count(session, Edition) == 3
    assert await count(session, UserWorkState) == 3

    entitlement = await one_entitlement(session, "292030")
    link = await session.get(EntitlementWork, (entitlement.id, 1))
    assert link is not None
    assert link.role is WorkLinkRole.PRIMARY
    assert link.created_by_run_id is not None
    # Nothing scored it: a stub is not a match.
    assert (link.match_layer, link.confidence) == (None, None)

    work = await session.get(Work, link.work_id)
    assert work is not None
    assert work.title == "The Witcher 3: Wild Hunt"
    # The display rule, so the card files under W.
    assert work.sort_title == "Witcher 3: Wild Hunt, The"
    assert work.is_matched is False
    # `ludamatch`'s output, and it lives in another repository until M2.
    assert work.normalised_title is None

    edition = await session.get(Edition, entitlement.edition_id)
    assert edition is not None
    assert (edition.name, edition.slug, edition.is_default) == ("Standard", "standard", True)
    assert edition.work_id == work.id


async def test_the_run_is_closed_with_its_counters(session: AsyncSession, account: Account) -> None:
    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert run.status is SyncStatus.SUCCESS
    assert run.trigger is SyncTrigger.MANUAL
    assert run.finished_at is not None
    assert (run.items_seen, run.items_added, run.items_updated) == (3, 3, 0)
    # A first run has nothing to sweep: everything it saw, it just created.
    assert run.items_removed == 0
    assert run.error_text is None
    assert run.account_id == account.id


async def test_the_run_names_the_reporting_provider(
    session: AsyncSession, account: Account
) -> None:
    """The reporter, not the account's provider — they differ for a Galaxy import."""

    steam = await make_provider(session)

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert run.provider_id == steam.id


async def test_a_provider_nothing_seeded_refuses_to_start(
    session: AsyncSession, account: Account
) -> None:
    with pytest.raises(SyncError, match="epic"):
        await sync_account(session, account=account, library=FakeLibrary(key="epic"))


async def test_a_second_run_of_the_same_library_duplicates_nothing(
    session: AsyncSession, account: Account
) -> None:
    """The upsert key doing its job, which is the difference between a sync and an import."""

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    first = await one_entitlement(session, "292030")
    first_seen_at, first_id = first.first_seen_at, first.id
    before = {
        model.__name__: await count(session, model)
        for model in (Entitlement, Work, Edition, EntitlementWork, UserWorkState, FieldProvenance)
    }

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    after = {
        model.__name__: await count(session, model)
        for model in (Entitlement, Work, Edition, EntitlementWork, UserWorkState, FieldProvenance)
    }
    assert after == before
    assert (run.items_seen, run.items_added, run.items_updated) == (3, 0, 3)

    again = await one_entitlement(session, "292030")
    assert again.id == first_id
    # "Owned since" has to survive every later run, so this one is never touched.
    assert again.first_seen_at == first_seen_at
    assert again.last_seen_at >= first_seen_at


async def test_a_provider_that_changed_its_mind_updates_in_place(
    session: AsyncSession, account: Account
) -> None:
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    await sync_account(
        session,
        account=account,
        library=FakeLibrary([owned("292030", "The Witcher 3: Wild Hunt", playtime_minutes=4000)]),
    )

    entitlement = await one_entitlement(session, "292030")
    assert entitlement.playtime_minutes == 4000
    rows = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == EntityType.ENTITLEMENT,
                FieldProvenance.entity_id == entitlement.id,
                FieldProvenance.field == "playtime_minutes",
            )
        )
    )
    # Updated in place: provenance is a snapshot of what each source currently
    # says, not a log of what it has ever said.
    assert [row.value for row in rows] == [4000]


async def test_provider_values_arrive_as_provenance_rows(
    session: AsyncSession, account: Account
) -> None:
    """Rule 9 from the outside: the columns hold what an effective row says they do."""

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    entitlement = await one_entitlement(session, "292030")
    rows = list(
        await session.scalars(
            select(FieldProvenance).where(FieldProvenance.entity_id == entitlement.id)
        )
    )
    asserted = {row.field: row for row in rows}
    assert set(asserted) == {"provider_item_id", "provider_title", "playtime_minutes"}
    for row in rows:
        assert row.entity_type is EntityType.ENTITLEMENT
        assert row.source_kind is SourceKind.PLATFORM_API
        assert row.source_ref == "steam"
        assert row.run_id == run.id
        assert row.is_effective is True

    assert asserted["playtime_minutes"].value == entitlement.playtime_minutes == 3247
    assert asserted["provider_title"].value == entitlement.provider_title


async def test_only_the_resolver_fills_a_resolved_column(
    session: AsyncSession, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 9 with the resolver taken away, which is the only way to prove it behaviourally.

    `playtime_minutes` is the field that shows it: unlike `provider_title` it is
    not seeded at insert, so if anything in the sync service wrote entity
    columns itself, 3247 would be on the row anyway.
    """

    async def refuse_to_decide(*args: object, **kwargs: object) -> dict[str, ScalarValue | None]:
        return {}

    monkeypatch.setattr(sync_module, "resolve", refuse_to_decide)

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    entitlement = await one_entitlement(session, "292030")
    assert entitlement.playtime_minutes is None
    stated = await session.scalar(
        select(FieldProvenance.value).where(
            FieldProvenance.entity_id == entitlement.id,
            FieldProvenance.field == "playtime_minutes",
        )
    )
    assert stated == 3247


def test_nothing_in_the_sync_service_assigns_to_a_resolved_column() -> None:
    """The same rule structurally, so it survives a change no runtime test covers.

    Only assignment targets are checked. A constructor keyword is the one
    legitimate way this module touches a registered field — `provider_title` is
    NOT NULL and the row has to exist before a provenance row can address it.

    The guard reads the variable name to know which entity is meant, since the
    AST carries no types; the assertion below fails loudly if a rename ever
    leaves it looking at nothing.
    """

    entities = {"entitlement", "work", "edition"}
    tree = ast.parse(Path(sync_module.__file__).read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert entities <= names, "the guard keys on variable names; rename it here too"

    written = {
        f"{node.value.id}.{node.attr}"
        for statement in ast.walk(tree)
        if isinstance(statement, ast.Assign | ast.AugAssign)
        for node in (statement.targets if isinstance(statement, ast.Assign) else [statement.target])
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id, node.attr) in STRATEGIES
    }
    assert written == set()


async def test_the_work_playtime_is_the_sum_of_its_entitlements(
    session: AsyncSession, account: Account
) -> None:
    """`sum`, not `max`: two accounts are two stretches of play, not two reports of one.

    The second link is written by hand because matching is M2 — this is the
    shape a merged stub leaves behind, and the aggregate has to be right the
    moment it exists.
    """

    second = await make_account(session, external_account_id="765611980")
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    await sync_account(
        session,
        account=second,
        library=FakeLibrary([owned("292030", "The Witcher 3: Wild Hunt", playtime_minutes=800)]),
    )

    witcher_on_steam = await session.scalar(
        select(Entitlement).where(
            Entitlement.account_id == account.id, Entitlement.provider_item_id == "292030"
        )
    )
    assert witcher_on_steam is not None
    link = await session.scalar(
        select(EntitlementWork).where(EntitlementWork.entitlement_id == witcher_on_steam.id)
    )
    assert link is not None
    other = await session.scalar(
        select(Entitlement).where(
            Entitlement.account_id == second.id, Entitlement.provider_item_id == "292030"
        )
    )
    assert other is not None
    session.add(
        EntitlementWork(entitlement_id=other.id, work_id=link.work_id, role=WorkLinkRole.GRANTED)
    )
    await session.flush()

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    state = await session.get(UserWorkState, (account.user_id, link.work_id))
    assert state is not None
    assert state.playtime_minutes == 4047


async def test_a_new_stub_starts_with_the_defaults_m1_does_not_compute(
    session: AsyncSession, account: Account
) -> None:
    """`platform_count` and `last_played_at` are M4's: `derived` and `latest` are deferred."""

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    state = await session.scalar(select(UserWorkState))
    assert state is not None
    assert state.platform_count == 0
    assert state.last_played_at is None


async def test_the_columns_no_strategy_governs_come_straight_from_the_provider(
    session: AsyncSession, account: Account
) -> None:
    """Nothing competes for these, so there is no provenance row and no ladder to run."""

    played = datetime(2025, 4, 18, 18, 13, 20, tzinfo=UTC)
    await sync_account(
        session,
        account=account,
        library=FakeLibrary(
            [
                owned(
                    "292030",
                    "The Witcher 3: Wild Hunt",
                    ownership_type=OwnershipType.FAMILY_SHARED,
                    item_kind=ItemKind.DLC,
                    last_played_at=played,
                    raw={"appid": 292030},
                )
            ]
        ),
    )

    entitlement = await one_entitlement(session, "292030")
    assert entitlement.ownership_type is OwnershipType.FAMILY_SHARED
    assert entitlement.item_kind is ItemKind.DLC
    assert entitlement.last_played_at == played
    assert entitlement.raw_payload == {"appid": 292030}
    assert entitlement.origin is EntitlementOrigin.SYNC


async def test_a_playtime_the_platform_omits_stays_null(
    session: AsyncSession, account: Account
) -> None:
    """Null is "no figure", which is not zero minutes played — the grid must not lie."""

    await sync_account(
        session,
        account=account,
        library=FakeLibrary([owned("620", "Portal 2", playtime_minutes=None)]),
    )

    entitlement = await one_entitlement(session, "620")
    assert entitlement.playtime_minutes is None
    state = await session.scalar(select(UserWorkState))
    assert state is not None
    # The aggregate is a number regardless: nothing played sums to nothing.
    assert state.playtime_minutes == 0


async def test_two_accounts_on_one_platform_own_the_game_separately(
    session: AsyncSession, account: Account
) -> None:
    """The upsert key starts with `account_id`, so a shared appid is two rows, not one."""

    second = await make_account(session, external_account_id="765611980")
    library = FakeLibrary([owned("292030", "The Witcher 3: Wild Hunt", playtime_minutes=10)])

    await sync_account(session, account=account, library=library)
    await sync_account(session, account=second, library=library)

    assert await count(session, Entitlement) == 2
    # Two stubs until a cascade layer merges them, which is the honest M1 state
    # and is written down in ADR-0015 as something users will report as a bug.
    assert await count(session, Work) == 2


async def test_a_manual_row_is_left_alone(session: AsyncSession, account: Account) -> None:
    """Rule 2. A disc copy or an unredeemed key is the user's assertion, not the platform's."""

    await make_user(session)
    manual = Entitlement(
        account_id=account.id,
        origin=EntitlementOrigin.MANUAL,
        provider_item_id=None,
        provider_title="Baldur's Gate II (disc)",
    )
    session.add(manual)
    await session.flush()
    before = manual.provider_title

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    await session.refresh(manual)
    assert manual.provider_title == before
    assert manual.origin is EntitlementOrigin.MANUAL
    assert manual.removed_at is None
    assert await count(session, FieldProvenance) == 9


async def test_a_game_the_platform_stopped_listing_is_marked_not_deleted(
    session: AsyncSession, account: Account
) -> None:
    """Rule 1. The row that survives is the only one holding data the platform never had.

    Playtime, status, rating and notes are the user's, and no platform can give
    them back. So absence marks the row and records the run that did it, and
    everything the user built on top of the entitlement stays exactly where it
    was.
    """

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    entitlement = await one_entitlement(session, "620")
    first_seen_at = entitlement.first_seen_at

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES[:1]))

    assert run.status is SyncStatus.SUCCESS
    assert run.items_removed == 2
    await session.refresh(entitlement)
    assert entitlement.removed_at is not None
    assert entitlement.removed_by_run_id == run.id
    # Never a DELETE: the row, its links and its own playtime all stay.
    assert await count(session, Entitlement) == 3
    assert await count(session, EntitlementWork) == 3
    assert entitlement.first_seen_at == first_seen_at
    # The one the platform still lists is untouched by the sweep.
    kept = await one_entitlement(session, "292030")
    assert (kept.removed_at, kept.removed_by_run_id) == (None, None)


async def test_a_removal_stops_counting_towards_the_work_total(
    session: AsyncSession, account: Account
) -> None:
    """ADR-0010: work-level aggregates come from non-removed entitlements only.

    The entitlement keeps its own 3247 minutes — they were played — while the
    work stops claiming them, so a restore puts the total back without having
    stored it anywhere.
    """

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    entitlement = await one_entitlement(session, "292030")
    link = await session.scalar(
        select(EntitlementWork).where(EntitlementWork.entitlement_id == entitlement.id)
    )
    assert link is not None
    state = await session.get(UserWorkState, (account.user_id, link.work_id))
    assert state is not None
    assert state.playtime_minutes == 3247

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES[1:]))

    await session.refresh(state)
    await session.refresh(entitlement)
    assert state.playtime_minutes == 0
    assert entitlement.playtime_minutes == 3247


async def test_a_game_that_comes_back_is_restored_where_it_was(
    session: AsyncSession, account: Account
) -> None:
    """What makes an outage cosmetic rather than a thousand-click repair.

    A removal is precautionary, and the platform listing the item again is the
    best evidence there is that it was. `first_seen_at` never moved, so "owned
    since" reads the same as if nothing had happened.
    """

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    entitlement = await one_entitlement(session, "620")
    first_seen_at, entitlement_id = entitlement.first_seen_at, entitlement.id
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES[:1]))
    await session.refresh(entitlement)
    assert entitlement.removed_at is not None

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    await session.refresh(entitlement)
    assert entitlement.id == entitlement_id
    assert (entitlement.removed_at, entitlement.removed_by_run_id) == (None, None)
    assert entitlement.first_seen_at == first_seen_at
    assert (run.items_added, run.items_updated, run.items_removed) == (0, 3, 0)


async def test_an_empty_library_sweeps_the_whole_account(
    session: AsyncSession, account: Account
) -> None:
    """Deliberate: this is what a platform saying "you own nothing" looks like.

    Telling it apart from a truncated response is the provider's job, not this
    one's — `SteamProvider` refuses a body shorter than the count Steam sent
    with it. And nothing is lost either way, which is the point of rule 1.
    """

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    run = await sync_account(session, account=account, library=FakeLibrary([]))

    assert run.items_removed == 3
    assert await count(session, Entitlement) == 3
    remaining = await session.scalar(
        select(func.count()).select_from(Entitlement).where(Entitlement.removed_at.is_(None))
    )
    assert remaining == 0


async def test_a_failed_run_marks_nothing_removed(
    session: AsyncSession, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that rule 1 exists for, injected after the sweep has already run.

    Failing earlier would prove only that the sweep never got its turn. Failing
    here means the removals were written and then rolled back, which is the
    guarantee itself: an Epic outage cannot empty the Epic library, because the
    same transaction carries both the removals and the `success`.
    """

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    async def collapse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("died after the sweep")

    monkeypatch.setattr(sync_module, "_aggregate", collapse)

    with pytest.raises(RuntimeError):
        await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES[:1]))

    run = await session.scalar(select(sync_module.SyncRun).order_by(sync_module.SyncRun.id.desc()))
    assert run is not None
    assert run.status is SyncStatus.FAILED
    assert run.items_removed == 0
    still_here = await session.scalar(
        select(func.count()).select_from(Entitlement).where(Entitlement.removed_at.is_(None))
    )
    assert still_here == 3


async def test_a_manual_row_survives_a_sweep_that_takes_everything_else(
    session: AsyncSession, account: Account
) -> None:
    """Rule 2, at the query that would otherwise be the one to break it.

    A disc copy corresponds to nothing on any platform, so every sync sees it as
    absent. Two predicates keep it: `origin != manual`, and the null guard that
    covers every nameless row. The second is what this test actually pins, since
    a CHECK constraint means a manual row can never reach the first one.
    """

    await make_user(session)
    manual = Entitlement(
        account_id=account.id,
        origin=EntitlementOrigin.MANUAL,
        provider_title="Baldur's Gate II (disc)",
    )
    session.add(manual)
    await session.flush()
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    run = await sync_account(session, account=account, library=FakeLibrary([]))

    await session.refresh(manual)
    assert manual.removed_at is None
    assert manual.removed_by_run_id is None
    # Only the three synced rows were swept; the manual one was never a candidate.
    assert run.items_removed == 3


async def test_an_imported_row_the_platform_cannot_name_survives_a_sweep(
    session: AsyncSession, account: Account
) -> None:
    """Absence is only evidence about rows the provider was asked about.

    A CSV import writes entitlements with no `provider_item_id` — the row says
    what the user owns, not what Steam calls it. Such a row is absent from every
    library response because it can never be present in one, so a sweep that
    went by absence alone would remove it on the first run and on every run
    after, and the restore in the removed view would not survive the next sync.
    """

    await make_user(session)
    imported = Entitlement(
        account_id=account.id,
        origin=EntitlementOrigin.IMPORT,
        provider_title="Beyond Good & Evil (from a Galaxy export)",
    )
    session.add(imported)
    await session.flush()

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    await session.refresh(imported)
    assert imported.removed_at is None
    assert run.items_removed == 0


async def test_a_library_larger_than_sqlite_can_bind_still_sweeps(
    session: AsyncSession, account: Account
) -> None:
    """A set difference in Python, not one bind parameter per owned item.

    Steam accounts past SQLite's ceiling of 32766 variables are rare and they
    exist, and `NOT IN (...)` over one does not merely run slowly: it raises
    inside the run's own transaction, so the run rolls back, reports `failed`,
    and does so identically on every retry until the library shrinks. The
    account is stuck rather than slow, which is why the size is a test and not a
    note in the perf issue.

    Driven straight at `_sweep`: a full sync of this many items would add
    minutes to the suite for the sake of a set difference that never touches the
    database.
    """

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    run = sync_module.SyncRun(
        provider_id=account.provider_id,
        account_id=account.id,
        trigger=SyncTrigger.MANUAL,
        status=SyncStatus.RUNNING,
    )
    session.add(run)
    await session.flush()
    huge = THREE_GAMES[:2] + [owned(f"filler-{index}", f"Game {index}") for index in range(32767)]

    known = await sync_module._known(session, account=account)
    stale = sync_module._sweep(run=run, known=known, items=huge)

    dota = await one_entitlement(session, "570")
    assert stale == [dota.id]
    assert run.items_removed == 1


async def test_a_manual_row_cannot_be_given_a_provider_item_id(
    session: AsyncSession, account: Account
) -> None:
    """ADR-0010's claim as a constraint, because prose does not hold a schema.

    The ADR says a manual row carries no `provider_item_id` and therefore cannot
    collide with the upsert key. That was a fact about rows nobody had inserted
    yet. One that did carry one would leave sync with two bad options — adopt
    the row and overwrite a disc copy the platform knows nothing about, or
    refuse the whole library over a single entry — so the row is refused
    instead, where the mistake is made rather than where it lands.

    `import` is deliberately outside the constraint: a re-imported CSV upserts
    on the same key, which is the behaviour `docs/schema.md` describes.
    """

    await make_user(session)

    with pytest.raises(IntegrityError, match="manual_has_no_provider_item_id"):
        async with session.begin_nested():
            session.add(
                Entitlement(
                    account_id=account.id,
                    origin=EntitlementOrigin.MANUAL,
                    provider_item_id="292030",
                    provider_title="The Witcher 3 (a key I never redeemed)",
                )
            )

    session.add(
        Entitlement(
            account_id=account.id,
            origin=EntitlementOrigin.IMPORT,
            provider_item_id="292030",
            provider_title="The Witcher 3",
        )
    )
    await session.flush()


async def test_an_empty_library_is_a_successful_run(
    session: AsyncSession, account: Account
) -> None:
    """An account that owns nothing is not a failure, and it must not look like one.

    The distinction is the provider's to make and it makes it: a private profile
    raises, an empty public library returns `[]`.
    """

    run = await sync_account(session, account=account, library=FakeLibrary([]))

    assert run.status is SyncStatus.SUCCESS
    assert (run.items_seen, run.items_added, run.items_updated) == (0, 0, 0)
    assert await count(session, Entitlement) == 0


async def test_a_provider_failure_is_a_status_not_an_exception(
    session: AsyncSession, account: Account
) -> None:
    """Rule 4: a caller syncing several accounts must not have to catch anything."""

    library = FakeLibrary(error=ProviderUnavailableError("steam answered 503 for /IPlayerService"))

    run = await sync_account(session, account=account, library=library)

    assert run.status is SyncStatus.FAILED
    assert run.finished_at is not None
    assert run.error_text is not None
    assert "503" in run.error_text
    assert await count(session, Entitlement) == 0


async def test_a_failure_partway_through_leaves_the_library_as_it_was(
    session: AsyncSession, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 1 at the transaction boundary, and rule 7 at the column that records it.

    A run that dies mid-write must commit nothing, and the message of an
    exception that is ours — not a `ProviderError`, whose contract forbids
    credentials — has no business being persisted at all.
    """

    async def collapse(*args: object, **kwargs: object) -> dict[str, ScalarValue | None]:
        raise ValueError("something with 0123456789ABCDEF in it")

    monkeypatch.setattr(sync_module, "resolve", collapse)

    with pytest.raises(ValueError):
        await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    run = await session.scalar(select(sync_module.SyncRun))
    assert run is not None
    assert run.status is SyncStatus.FAILED
    assert run.error_text == "ValueError"
    # And the same string on the provider row, for the same reason: rule 7 does
    # not stop at the column the run happens to write.
    steam = await make_provider(session)
    await session.refresh(steam)
    assert steam.last_error == "ValueError"
    assert await count(session, Entitlement) == 0
    assert await count(session, Work) == 0
    # The rollback is right to take `items_added` — after it, nothing was added.
    # It is wrong to take this one: the provider did hand over three items, and
    # "3 seen, 0 added" is what tells an outage apart from a bad write.
    assert (run.items_seen, run.items_added) == (3, 0)


async def test_a_cancelled_run_does_not_stay_running_forever(
    session: AsyncSession, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`CancelledError` stopped being an `Exception` in 3.8, and the run row pays for it.

    A caller putting a deadline around one account — `asyncio.wait_for`, an
    APScheduler job timeout — would otherwise leave `running` behind with no
    `finished_at`, which is the one failure nobody is watching for: the status
    panel shows a sync that never ends rather than one that failed.

    The cancellation itself is still re-raised; the caller sees `TimeoutError`
    from `wait_for` either way, so a loop over further accounts carries on.
    """

    async def dawdle(*args: object, **kwargs: object) -> Work:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    monkeypatch.setattr(sync_module, "_stub", dawdle)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            sync_account(session, account=account, library=FakeLibrary(THREE_GAMES)),
            timeout=0.1,
        )

    run = await session.scalar(select(sync_module.SyncRun))
    assert run is not None
    assert run.status is SyncStatus.FAILED
    assert run.finished_at is not None
    assert run.error_text == "CancelledError"
    assert run.items_seen == 3
    assert await count(session, Entitlement) == 0


async def test_a_blank_title_still_makes_a_findable_card(
    session: AsyncSession, account: Account
) -> None:
    """Steam has app ids whose name comes back empty; a str is all the client promises.

    A work titled "" is a card with no text and no sort key — unusable in the
    grid and unfindable in a search. The id is not a title and does not pretend
    to be one, but it can be renamed in M3 and matched in M2, which an empty
    string can be neither of.
    """

    await sync_account(session, account=account, library=FakeLibrary([owned("228980", "   ")]))

    work = await session.scalar(select(Work))
    assert work is not None
    assert (work.title, work.sort_title) == ("228980", "228980")
    # The platform's own blank is kept where it belongs: it is what Steam said.
    entitlement = await one_entitlement(session, "228980")
    assert entitlement.provider_title == "   "


async def test_a_successful_run_reports_the_provider_healthy(
    session: AsyncSession, account: Account
) -> None:
    steam = await make_provider(session)

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    await session.refresh(steam)
    await session.refresh(account)
    assert steam.status is SyncStatus.SUCCESS
    assert steam.last_error is None
    assert steam.last_success_at == run.finished_at
    assert account.last_success_at == run.finished_at


async def test_a_failure_records_itself_without_erasing_the_last_success(
    session: AsyncSession, account: Account
) -> None:
    """ "Never worked" and "worked this morning" are different problems for the user."""

    steam = await make_provider(session)
    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    await session.refresh(steam)
    worked_at = steam.last_success_at

    await sync_account(
        session,
        account=account,
        library=FakeLibrary(error=ProviderUnavailableError("steam answered 503")),
    )

    await session.refresh(steam)
    assert steam.status is SyncStatus.FAILED
    assert steam.last_error == "steam answered 503"
    assert steam.last_success_at == worked_at


async def test_a_successful_sync_sweeps_only_its_own_account(
    session: AsyncSession, account: Account
) -> None:
    """Rule 4 in the direction that destroys data rather than merely annoying.

    A run's evidence covers exactly one account. Steam listing three games says
    nothing at all about what the GOG account owns, so the sweep is scoped to
    the account before anything else — unscoped it would mark every other
    library removed on every sync, which is the failure rule 1 is written
    against arriving through the door rule 4 is meant to hold shut.
    """

    gog_account = await make_account(session, key="gog")
    # A different catalogue on purpose: with the same ids on both accounts, an
    # unscoped sweep would find nothing to remove and the test would pass while
    # the bug sat there.
    gog_library = [
        owned("1495134320", "The Witcher 3: Wild Hunt GOTY"),
        owned("1207658930", "Baldur's Gate II"),
        owned("1207666073", "Planescape: Torment"),
    ]
    await sync_account(session, account=gog_account, library=FakeLibrary(gog_library, key="gog"))

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert run.status is SyncStatus.SUCCESS
    assert run.items_removed == 0
    intact = await session.scalar(
        select(func.count())
        .select_from(Entitlement)
        .where(Entitlement.account_id == gog_account.id, Entitlement.removed_at.is_(None))
    )
    assert intact == 3


async def test_one_platform_failing_leaves_the_other_alone(
    session: AsyncSession, account: Account
) -> None:
    """Rule 4, which is why the health columns are per provider and not a global flag.

    An Epic outage that greyed out the Steam library, or wiped its rows, would
    make the whole catalogue only as reliable as its least reliable source.
    """

    gog_account = await make_account(session, key="gog")
    gog = await make_provider(session, key="gog")
    steam = await make_provider(session)
    await sync_account(session, account=gog_account, library=FakeLibrary(THREE_GAMES, key="gog"))
    await session.refresh(gog)
    gog_worked_at = gog.last_success_at

    await sync_account(
        session,
        account=account,
        library=FakeLibrary(error=ProviderUnavailableError("steam answered 503")),
    )

    await session.refresh(gog)
    await session.refresh(steam)
    assert (steam.status, gog.status) == (SyncStatus.FAILED, SyncStatus.SUCCESS)
    assert gog.last_error is None
    assert gog.last_success_at == gog_worked_at
    # And its rows: a failing Steam run swept nothing, on either account.
    intact = await session.scalar(
        select(func.count())
        .select_from(Entitlement)
        .where(Entitlement.account_id == gog_account.id, Entitlement.removed_at.is_(None))
    )
    assert intact == 3


@pytest.fixture
async def steam(account: Account) -> AsyncIterator[SteamProvider]:
    credentials = SteamCredentials(api_key="not-a-real-key", steam_id="765611979")
    async with httpx.AsyncClient() as client:
        yield SteamProvider(credentials, client)


@respx.mock
async def test_a_recorded_steam_library_lands_end_to_end(
    session: AsyncSession, account: Account, steam: SteamProvider
) -> None:
    """The two halves joined: the recorded response, through the client, into the schema."""

    respx.get(OWNED_GAMES_URL).mock(
        return_value=httpx.Response(
            200, json=json.loads((FIXTURES / "owned_games.json").read_text())
        )
    )

    run = await sync_account(session, account=account, library=steam)

    assert run.status is SyncStatus.SUCCESS
    assert run.items_added == 3
    titles = list(await session.scalars(select(Work.sort_title).order_by(Work.sort_title)))
    assert titles == ["Dota 2", "Portal 2", "Witcher 3: Wild Hunt, The"]
    entitlement = await one_entitlement(session, "292030")
    assert entitlement.playtime_minutes == 3247
    assert entitlement.raw_payload is not None
    assert entitlement.raw_payload["appid"] == 292030


async def test_a_second_run_of_one_account_is_refused(
    session: AsyncSession, account: Account
) -> None:
    """Not a check-then-act — the index is the guard, and this is its answer.

    Two overlapping runs would both upsert the same items and both sweep, one
    marking `removed_at` on rows the other is in the middle of seeing. The user
    who double-clicks and the scheduler firing over an unfinished run are the
    two ways it happens.
    """

    session.add(
        sync_module.SyncRun(
            provider_id=account.provider_id,
            account_id=account.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
        )
    )
    await session.commit()

    with pytest.raises(sync_module.SyncInProgressError, match="already syncing"):
        await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert await count(session, Entitlement) == 0


async def test_a_finished_run_blocks_nothing(session: AsyncSession, account: Account) -> None:
    """The index is partial for this reason: yesterday's runs must not pile up against it."""

    for _ in range(3):
        await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert await count(session, sync_module.SyncRun) == 3


async def test_one_account_syncing_does_not_block_another(session: AsyncSession) -> None:
    """Rule 4, at the one place a shared constraint could quietly break it."""

    steam = await make_account(session, "steam")
    gog = await make_account(session, "gog", external_account_id="gog-42")
    session.add(
        sync_module.SyncRun(
            provider_id=steam.provider_id,
            account_id=steam.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
        )
    )
    await session.commit()

    run = await sync_account(session, account=gog, library=FakeLibrary(THREE_GAMES, key="gog"))

    assert run.status is SyncStatus.SUCCESS


async def test_metadata_runs_carry_no_account_and_never_collide(session: AsyncSession) -> None:
    """IGDB and RAWG sync no account, so several of their runs are open by design.

    The `account_id IS NOT NULL` half of the index predicate cannot be tested
    apart from this: both SQLite and PostgreSQL already treat nulls in a unique
    index as distinct, so dropping it changes nothing either dialect does. It is
    written anyway, because "metadata providers are outside this rule" should be
    readable in the predicate rather than deduced from two dialects agreeing.
    """

    provider = await make_provider(session, key="igdb")
    for _ in range(2):
        session.add(
            sync_module.SyncRun(
                provider_id=provider.id,
                account_id=None,
                trigger=SyncTrigger.SCHEDULED,
                status=SyncStatus.RUNNING,
            )
        )
    await session.commit()

    assert await count(session, sync_module.SyncRun) == 2


async def test_an_abandoned_run_stops_blocking_the_account(
    session: AsyncSession, account: Account
) -> None:
    """One killed process must not lock the account out for good.

    `_close` covers cancellation and every exception, so what reaches this is a
    hard kill between the first commit and the second: the row says `running`
    and nothing is left alive to finish it.
    """

    orphan = sync_module.SyncRun(
        provider_id=account.provider_id,
        account_id=account.id,
        trigger=SyncTrigger.MANUAL,
        status=SyncStatus.RUNNING,
        started_at=utcnow() - sync_module.ORPHAN_AFTER - timedelta(minutes=1),
    )
    session.add(orphan)
    await session.commit()

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert run.status is SyncStatus.SUCCESS
    await session.refresh(orphan)
    # `failed`, never `success`: a run nobody finished must stay incapable of
    # owning a removal (rule 1).
    assert orphan.status is SyncStatus.FAILED
    assert orphan.finished_at is not None
    assert orphan.error_text == "abandoned; no process was left to finish it"


async def test_a_run_that_only_started_a_minute_ago_still_blocks(
    session: AsyncSession, account: Account
) -> None:
    """The threshold is generous on purpose; it must not be so generous it is absent."""

    session.add(
        sync_module.SyncRun(
            provider_id=account.provider_id,
            account_id=account.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
            started_at=utcnow() - timedelta(minutes=1),
        )
    )
    await session.commit()

    with pytest.raises(sync_module.SyncInProgressError):
        await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))


async def test_an_integrity_error_it_cannot_confirm_is_not_relabelled(
    session: AsyncSession, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Already syncing" is a claim about one index, so it is only made when true.

    A real collision, with the confirming lookup finding nothing — which is what
    a violation of some *other* constraint on the row looks like from here. A
    broken foreign key reported as a concurrency answer would be looked for in
    entirely the wrong place.
    """

    session.add(
        sync_module.SyncRun(
            provider_id=account.provider_id,
            account_id=account.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
        )
    )
    await session.commit()

    async def blind(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(sync_module, "_open_run", blind)

    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))


async def test_a_blocker_that_finishes_first_gets_out_of_the_way(
    session: AsyncSession, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between the collision and the question that confirms it.

    The rival run finished in it, so by the time anyone looks there is nothing
    in the way. Refusing over a run that no longer exists would be a 409 about
    a fact that stopped being true, so the second attempt just goes ahead.
    """

    blocker = sync_module.SyncRun(
        provider_id=account.provider_id,
        account_id=account.id,
        trigger=SyncTrigger.MANUAL,
        status=SyncStatus.RUNNING,
    )
    session.add(blocker)
    await session.commit()

    # Read now, not inside the replacement: by then the rollback has expired the
    # instance, and the lazy load is the very trap this module keeps stepping in.
    blocker_id = blocker.id

    async def finishes_first(inner: AsyncSession, account_id: int) -> None:
        await inner.execute(
            update(sync_module.SyncRun)
            .where(sync_module.SyncRun.id == blocker_id)
            .values(status=SyncStatus.SUCCESS, finished_at=utcnow())
        )
        await inner.commit()

    monkeypatch.setattr(sync_module, "_open_run", finishes_first)

    run = await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    assert run.status is SyncStatus.SUCCESS
    assert await count(session, Entitlement) == 3


async def test_the_reclaim_rides_on_the_run_it_makes_room_for(
    session: AsyncSession, account: Account
) -> None:
    """One transaction, not two: the update commits with the insert or not at all.

    A separate commit here would be a second fsync on every sync of every
    account, to close a row that is almost never there.
    """

    session.add(
        sync_module.SyncRun(
            provider_id=account.provider_id,
            account_id=account.id,
            trigger=SyncTrigger.MANUAL,
            status=SyncStatus.RUNNING,
            started_at=utcnow() - sync_module.ORPHAN_AFTER - timedelta(minutes=1),
        )
    )
    await session.commit()
    commits = 0
    original = AsyncSession.commit

    async def counted(inner: AsyncSession) -> None:
        nonlocal commits
        commits += 1
        await original(inner)

    monkeypatch_target = AsyncSession
    monkeypatch_target.commit = counted
    try:
        await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))
    finally:
        monkeypatch_target.commit = original

    # One to open the run, one to close it. The reclaim adds none.
    assert commits == 2


# Measured at 9.05, and the floor is nine of those: an ORM insert whose
# generated id is needed back cannot be batched — the driver reports one
# `lastrowid` per execution — and a first run inserts five such rows per game
# (the entitlement, its three provenance rows, the work and its edition), on top
# of the provenance read, the entitlement update and the winners update.
#
# Ten rather than nine so the number is not pinned to a SQLAlchemy patch
# release, and not eleven: the slack is under one statement, so anything that
# reintroduces a per-item query fails here.
PER_ITEM_CEILING = 10


async def statements_for(
    database: Database, session: AsyncSession, account: Account, items: list[LibraryItem]
) -> list[str]:
    """Every statement one run sends, in order."""

    seen: list[str] = []

    def tally(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        seen.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", tally)
    try:
        await sync_account(session, account=account, library=FakeLibrary(items))
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", tally)
    return seen


def library(size: int, offset: int = 0) -> list[LibraryItem]:
    return [owned(str(offset + index), f"Game {offset + index}") for index in range(size)]


async def test_a_run_costs_a_fixed_number_of_statements_per_game(
    db: Database, session: AsyncSession, account: Account
) -> None:
    """#23, pinned as a slope so the next per-item query fails here rather than on a NAS.

    Everything that can be asked once for the whole library is: the account's
    existing rows, the sweep's set difference, the works to re-total and their
    state rows. What is left per game is writes SQLite will not batch.

    A slope rather than a total, because opening and closing a run costs the
    same whether it syncs four games or four hundred, and that fixed part is not
    what the issue is about.
    """

    second = await make_account(session, external_account_id="765611980")

    small = await statements_for(db, session, account, library(4))
    large = await statements_for(db, session, second, library(44, offset=1000))

    growth = (len(large) - len(small)) / 40
    assert growth <= PER_ITEM_CEILING, f"{growth:.2f} statements per game"


async def test_a_run_reads_no_more_than_one_row_per_game(
    db: Database, session: AsyncSession, account: Account
) -> None:
    """Reads are the half a network makes expensive, so they get their own ceiling.

    One per game is the provenance read, which is the last per-item query left.
    Every other question — which rows this account already has, which works to
    re-total, what their state rows say — is asked once for the whole library.
    """

    second = await make_account(session, external_account_id="765611980")

    def reads(statements: list[str]) -> int:
        return sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)

    small = reads(await statements_for(db, session, account, library(4)))
    large = reads(await statements_for(db, session, second, library(44, offset=1000)))

    assert (large - small) / 40 <= 1


async def test_an_item_the_provider_lists_twice_is_still_one_row(
    session: AsyncSession, account: Account
) -> None:
    """A repeated entry is the provider repeating itself, not a second copy owned.

    The run reads the account's rows once now rather than once per item, so the
    row made for the first mention has to go back into that map. Without it the
    second mention inserts a duplicate, the unique index refuses it, and a
    harmless quirk in a response becomes a failed run.
    """

    twice = [owned("570", "Dota 2"), owned("570", "Dota 2", playtime_minutes=12)]

    run = await sync_account(session, account=account, library=FakeLibrary(twice))

    assert run.status is SyncStatus.SUCCESS
    assert await count(session, Entitlement) == 1
    assert (run.items_added, run.items_updated) == (1, 1)
    # The second mention is the same source changing its mind, written to the
    # same provenance row.
    assert (await one_entitlement(session, "570")).playtime_minutes == 12


async def test_more_works_than_one_in_clause_holds_still_get_their_totals(
    session: AsyncSession, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The aggregate names its ids a batch at a time, and the batches have to add up.

    The limit is lowered rather than the library raised: a real one would need a
    thousand games to reach the second batch, and what is under test is that
    there is a second batch at all, not how long it takes to build one.
    """

    monkeypatch.setattr(queries, "BIND_LIMIT", 2)

    await sync_account(session, account=account, library=FakeLibrary(THREE_GAMES))

    totals = {
        title: minutes
        for title, minutes in await session.execute(
            select(Work.title, UserWorkState.playtime_minutes).join(
                UserWorkState, UserWorkState.work_id == Work.id
            )
        )
    }
    assert totals == {"The Witcher 3: Wild Hunt": 3247, "Portal 2": 0, "Dota 2": 12}
