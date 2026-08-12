from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import (
    EntitlementOrigin,
    EntityType,
    ItemKind,
    OwnershipType,
    PlayStatus,
    ProviderKind,
    SourceKind,
    WorkLinkRole,
)
from ludarium.models import (
    Account,
    AppUser,
    Edition,
    Entitlement,
    EntitlementWork,
    FieldProvenance,
    Provider,
    UserWorkState,
    Work,
)
from ludarium.titles import sort_title


async def make_account(session: AsyncSession, key: str = "steam") -> Account:
    session.add(AppUser(username="owner", password_hash="not-a-hash"))
    provider = Provider(
        key=key,
        kind=ProviderKind.PLATFORM,
        source_kind=SourceKind.PLATFORM_API,
        display_name=key.title(),
    )
    session.add(provider)
    await session.flush()
    account = Account(provider_id=provider.id, external_account_id="765611979", label="Main")
    session.add(account)
    await session.flush()
    return account


async def make_work(session: AsyncSession, title: str = "The Witcher 3: Wild Hunt") -> Work:
    work = Work(title=title, sort_title=sort_title(title))
    session.add(work)
    await session.flush()
    return work


async def make_entitlement(
    session: AsyncSession, account: Account, *, provider_item_id: str | None = "292030"
) -> Entitlement:
    entitlement = Entitlement(
        account_id=account.id,
        provider_item_id=provider_item_id,
        provider_title="The Witcher 3: Wild Hunt",
    )
    session.add(entitlement)
    await session.flush()
    return entitlement


async def test_round_trip_with_defaults(session: AsyncSession) -> None:
    account = await make_account(session)
    work = await make_work(session)
    session.add(Edition(work_id=work.id, name="Standard", slug="standard", is_default=True))
    await session.flush()
    entitlement = await make_entitlement(session, account)
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=work.id))
    session.add(UserWorkState(work_id=work.id))
    await session.commit()
    session.expunge_all()

    stored_work = (await session.scalars(select(Work))).one()
    stored_entitlement = (await session.scalars(select(Entitlement))).one()
    stored_link = (await session.scalars(select(EntitlementWork))).one()
    stored_state = (await session.scalars(select(UserWorkState))).one()

    assert stored_work.sort_title == "Witcher 3: Wild Hunt, The"
    assert stored_work.item_kind is ItemKind.GAME
    assert stored_work.is_matched is False
    # Nothing in M1 writes it; ludamatch does, in M2.
    assert stored_work.normalised_title is None
    assert stored_entitlement.origin is EntitlementOrigin.SYNC
    assert stored_entitlement.ownership_type is OwnershipType.OWNED
    assert stored_entitlement.removed_at is None
    assert stored_link.role is WorkLinkRole.PRIMARY
    assert stored_link.confidence is None
    assert stored_state.user_id == 1
    assert stored_state.play_status is PlayStatus.NOT_STARTED
    assert (stored_state.playtime_minutes, stored_state.platform_count) == (0, 0)


async def test_a_json_payload_survives_the_round_trip(session: AsyncSession) -> None:
    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    entitlement.raw_payload = {"appid": 292030, "playtime_forever": 5432}
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(Entitlement))).one()

    assert stored.raw_payload == {"appid": 292030, "playtime_forever": 5432}


async def test_one_entitlement_per_account_and_provider_item(session: AsyncSession) -> None:
    account = await make_account(session)
    await make_entitlement(session, account)

    session.add(
        Entitlement(account_id=account.id, provider_item_id="292030", provider_title="Duplicate")
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_entitlements_without_a_provider_item_do_not_collide(session: AsyncSession) -> None:
    """Manual rows carry no provider item, and there can be any number of them (rule 2)."""

    account = await make_account(session, key="manual")
    await make_entitlement(session, account, provider_item_id=None)
    await make_entitlement(session, account, provider_item_id=None)

    await session.commit()

    assert (await session.scalars(select(func.count()).select_from(Entitlement))).one() == 2


async def test_an_entitlement_has_one_primary_work(session: AsyncSession) -> None:
    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    first = await make_work(session)
    second = await make_work(session, title="Hearts of Stone")
    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=first.id))
    await session.flush()

    session.add(EntitlementWork(entitlement_id=entitlement.id, work_id=second.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_bundle_grants_several_works(session: AsyncSession) -> None:
    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    base = await make_work(session)
    expansion = await make_work(session, title="Hearts of Stone")
    session.add_all(
        [
            EntitlementWork(entitlement_id=entitlement.id, work_id=base.id),
            EntitlementWork(
                entitlement_id=entitlement.id, work_id=expansion.id, role=WorkLinkRole.GRANTED
            ),
        ]
    )

    await session.commit()

    assert (await session.scalars(select(func.count()).select_from(EntitlementWork))).one() == 2


async def test_one_effective_provenance_row_per_field(session: AsyncSession) -> None:
    work = await make_work(session)
    session.add(
        FieldProvenance(
            entity_type=EntityType.WORK,
            entity_id=work.id,
            field="title",
            source_kind=SourceKind.METADATA_PROVIDER,
            source_ref="igdb",
            value='"The Witcher 3: Wild Hunt"',
            is_effective=True,
        )
    )
    await session.flush()

    session.add(
        FieldProvenance(
            entity_type=EntityType.WORK,
            entity_id=work.id,
            field="title",
            source_kind=SourceKind.PLATFORM_API,
            source_ref="steam",
            value='"The Witcher 3"',
            is_effective=True,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_several_sources_may_lose_the_same_field(session: AsyncSession) -> None:
    """The point of the table: every assertion is kept, one of them wins."""

    work = await make_work(session)
    session.add_all(
        [
            FieldProvenance(
                entity_type=EntityType.WORK,
                entity_id=work.id,
                field="title",
                source_kind=SourceKind.PLATFORM_API,
                source_ref="steam",
                value='"The Witcher 3"',
            ),
            FieldProvenance(
                entity_type=EntityType.WORK,
                entity_id=work.id,
                field="title",
                source_kind=SourceKind.PLATFORM_API,
                source_ref="gog",
                value='"The Witcher 3: Wild Hunt"',
            ),
        ]
    )

    await session.commit()

    assert (await session.scalars(select(func.count()).select_from(FieldProvenance))).one() == 2


async def test_one_provenance_row_per_source_per_field(session: AsyncSession) -> None:
    work = await make_work(session)
    for _ in range(2):
        session.add(
            FieldProvenance(
                entity_type=EntityType.WORK,
                entity_id=work.id,
                field="release_year",
                source_kind=SourceKind.PLATFORM_API,
                source_ref="steam",
                value="2015",
            )
        )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_one_work_per_igdb_anchor(session: AsyncSession) -> None:
    first = await make_work(session)
    first.igdb_id = 1942
    await session.flush()

    second = await make_work(session, title="The Witcher III")
    second.igdb_id = 1942
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_unmatched_works_do_not_collide(session: AsyncSession) -> None:
    """Stubs have no anchor, and there are a great many of them before M2."""

    await make_work(session)
    await make_work(session, title="Portal 2")

    await session.commit()

    assert (await session.scalars(select(func.count()).select_from(Work))).one() == 2


async def test_one_edition_slug_per_work(session: AsyncSession) -> None:
    work = await make_work(session)
    other = await make_work(session, title="Portal 2")
    session.add_all(
        [
            Edition(work_id=work.id, name="Game of the Year", slug="goty"),
            # The same slug under a different work is fine.
            Edition(work_id=other.id, name="Game of the Year", slug="goty"),
        ]
    )
    await session.flush()

    session.add(Edition(work_id=work.id, name="GOTY", slug="goty"))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.parametrize("rating", [0, 11])
async def test_a_rating_outside_one_to_ten_is_refused(session: AsyncSession, rating: int) -> None:
    work = await make_work(session)
    session.add(UserWorkState(work_id=work.id, rating=rating))

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_deleting_a_work_takes_its_dependent_rows(session: AsyncSession) -> None:
    account = await make_account(session)
    work = await make_work(session)
    entitlement = await make_entitlement(session, account)
    session.add_all(
        [
            Edition(work_id=work.id, name="Standard", slug="standard"),
            EntitlementWork(entitlement_id=entitlement.id, work_id=work.id),
            UserWorkState(work_id=work.id),
        ]
    )
    await session.commit()

    await session.execute(text("DELETE FROM work WHERE id = :id"), {"id": work.id})

    for model in (Edition, EntitlementWork, UserWorkState):
        assert (await session.scalars(select(func.count()).select_from(model))).one() == 0
    # The entitlement is not a dependent row: what the user owns survives the
    # work it was matched to (rule 1).
    assert (await session.scalars(select(func.count()).select_from(Entitlement))).one() == 1


async def test_an_account_with_entitlements_cannot_be_deleted(session: AsyncSession) -> None:
    account = await make_account(session)
    await make_entitlement(session, account)
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM account WHERE id = :id"), {"id": account.id})


async def test_a_run_that_removed_an_entitlement_cannot_be_deleted(session: AsyncSession) -> None:
    """The audit trail behind rule 1 is only worth having if it cannot be orphaned."""

    account = await make_account(session)
    entitlement = await make_entitlement(session, account)
    await session.execute(
        text("INSERT INTO sync_run (provider_id, trigger) VALUES (:provider_id, 'scheduled')"),
        {"provider_id": account.provider_id},
    )
    run_id = (await session.execute(text("SELECT id FROM sync_run"))).scalar_one()
    entitlement.removed_at = datetime.now(UTC)
    entitlement.removed_by_run_id = run_id
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM sync_run WHERE id = :id"), {"id": run_id})
