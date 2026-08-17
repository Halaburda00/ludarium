import ast
import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import make_account, make_provider, make_user
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import sync as sync_module
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
from ludarium.models.types import ScalarValue
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
    # Nothing marks anything removed yet; that is issue #8.
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
