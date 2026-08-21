import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.db import Database
from ludarium.enums import LicenceClass, ProviderKind, SourceKind, SyncStatus
from ludarium.models import Provider
from ludarium.seed import ProviderSpec, seed_providers


def test_every_provider_column_is_either_seeded_or_runtime() -> None:
    """Rule 4 as a mechanism: a new column has to be put in one bucket, not both.

    `test_seeding_leaves_runtime_state_alone` only guards the four columns it
    names; this fails the moment `Provider` grows a fifth.
    """

    runtime = {"enabled", "status", "last_success_at", "last_error"}
    columns = {column.name for column in Provider.__table__.columns} - {"id"}

    assert columns == set(ProviderSpec.__dataclass_fields__) | runtime


async def test_seeds_steam_and_manual(session: AsyncSession) -> None:
    await seed_providers(session)

    providers = {provider.key: provider for provider in await session.scalars(select(Provider))}

    assert set(providers) == {"steam", "manual"}
    steam = providers["steam"]
    assert steam.kind is ProviderKind.PLATFORM
    assert steam.source_kind is SourceKind.PLATFORM_API
    assert steam.licence_class is LicenceClass.REDISTRIBUTABLE
    assert steam.store_url_template == "https://store.steampowered.com/app/{id}"
    manual = providers["manual"]
    assert manual.kind is ProviderKind.MANUAL
    assert manual.source_kind is SourceKind.MANUAL
    assert manual.store_url_template is None


async def test_seeding_twice_changes_nothing(session: AsyncSession) -> None:
    await seed_providers(session)
    before = {provider.key: provider.id for provider in await session.scalars(select(Provider))}

    await seed_providers(session)

    after = {provider.key: provider.id for provider in await session.scalars(select(Provider))}
    assert after == before


async def test_seeding_leaves_runtime_state_alone(session: AsyncSession) -> None:
    await seed_providers(session)
    steam = (await session.scalars(select(Provider).where(Provider.key == "steam"))).one()
    last_success_at = datetime.now(UTC)
    steam.enabled = False
    steam.status = SyncStatus.FAILED
    steam.last_success_at = last_success_at
    steam.last_error = "429 from the Steam API"
    await session.commit()

    await seed_providers(session)

    steam = (await session.scalars(select(Provider).where(Provider.key == "steam"))).one()
    assert steam.enabled is False
    assert steam.status is SyncStatus.FAILED
    assert steam.last_success_at == last_success_at
    assert steam.last_error == "429 from the Steam API"


async def test_seeding_brings_a_stale_row_back_in_step(session: AsyncSession) -> None:
    await seed_providers(session)
    steam = (await session.scalars(select(Provider).where(Provider.key == "steam"))).one()
    steam.display_name = "Steem"
    steam.store_url_template = None
    await session.commit()

    await seed_providers(session)

    steam = (await session.scalars(select(Provider).where(Provider.key == "steam"))).one()
    assert steam.display_name == "Steam"
    assert steam.store_url_template == "https://store.steampowered.com/app/{id}"


async def test_a_second_instance_seeds_behind_the_first(db: Database) -> None:
    """Check-then-act, and the transaction is the whole of the answer (ADR-0017).

    Start-up takes a writing session, so a second instance blocks at `BEGIN
    IMMEDIATE` rather than reading a table the first is about to fill. Held
    here with an uncommitted insert: the seed cannot begin until it lands, and
    then reads it — which is what a re-read after the insert would have told it,
    one round-trip earlier.

    That it corrects the stale `display_name` is the proof it read the winner's
    row rather than inserting a second one of its own.
    """

    async with db.writing_session_factory() as holder:
        holder.add(
            Provider(
                key="steam",
                kind=ProviderKind.PLATFORM,
                source_kind=SourceKind.PLATFORM_API,
                display_name="Steem",
            )
        )
        await holder.flush()

        async def seed() -> None:
            async with db.writing_session_factory() as session:
                await seed_providers(session)

        second = asyncio.create_task(seed())
        await asyncio.sleep(0.05)
        assert not second.done()

        await holder.commit()
        await second

    async with db.session_factory() as reader:
        providers = {row.key: row.display_name for row in await reader.scalars(select(Provider))}
    assert providers == {"steam": "Steam", "manual": "Manual entry"}
