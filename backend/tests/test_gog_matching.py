import pytest
import respx
from conftest import make_account
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_matching import Igdb, client, own, read, work_of

from ludarium.db import Database
from ludarium.enrichment import enrich
from ludarium.enums import EntityType, MatchLayer, SyncStatus
from ludarium.matching import anchor_gog_works, anchor_steam_works
from ludarium.models import Account, EntitlementWork, ExternalId
from ludarium.providers import IgdbClient
from ludarium.seed import seed_providers

__all__ = ["client"]

# IGDB's `external_game_sources` id for GOG, as `ludamatch` reads it.
GOG_SOURCE = 5


@pytest.fixture
async def gog(session: AsyncSession) -> Account:
    await seed_providers(session)
    account = await make_account(session, "gog", external_account_id="48000000000000001")
    await session.commit()
    return account


async def anchor_gog(db: Database, client: IgdbClient) -> SyncStatus:
    return (await enrich(db, provider="igdb", step=anchor_gog_works(client))).status


@respx.mock
async def test_a_gog_game_is_anchored_by_its_product_id(
    db: Database, session: AsyncSession, gog: Account, client: IgdbClient
) -> None:
    igdb = Igdb(
        [{"game": 1942, "uid": "1495134320", "source": GOG_SOURCE}],
        {1942: "The Witcher 3: Wild Hunt"},
    )
    igdb.mount()
    stub = await own(session, gog, "1495134320", "The Witcher 3: Wild Hunt — Remastered")
    await session.commit()

    assert await anchor_gog(db, client) is SyncStatus.SUCCESS

    work = await work_of(db, stub.id)
    assert (work.igdb_id, work.is_matched, work.title) == (1942, True, "The Witcher 3: Wild Hunt")
    link = await read(db, lambda reader: reader.scalar(select(EntitlementWork)))
    assert link.match_layer is MatchLayer.HARD_ID


@respx.mock
async def test_a_gog_copy_of_a_game_held_through_steam_folds_into_it(
    db: Database, session: AsyncSession, gog: Account, client: IgdbClient
) -> None:
    steam = await make_account(session)
    Igdb(
        [
            {"game": 1942, "uid": "292030"},
            {"game": 1942, "uid": "1495134320", "source": GOG_SOURCE},
        ],
        {1942: "The Witcher 3: Wild Hunt"},
    ).mount()
    on_steam = await own(session, steam, "292030", "The Witcher 3: Wild Hunt")
    on_gog = await own(session, gog, "1495134320", "The Witcher 3: Wild Hunt — Remastered")
    await session.commit()

    await enrich(db, provider="igdb", step=anchor_steam_works(client))
    assert await anchor_gog(db, client) is SyncStatus.SUCCESS

    links = await read(
        db, lambda reader: reader.execute(select(EntitlementWork.work_id, EntitlementWork.role))
    )
    # Both copies now reach the work Steam anchored; the GOG stub is gone.
    assert {work_id for work_id, _ in links} == {on_steam.id}
    assert await read(db, lambda reader: reader.get(type(on_gog), on_gog.id)) is None


@respx.mock
async def test_a_gog_only_game_keeps_igdb_s_steam_appid_for_its_score(
    db: Database, session: AsyncSession, gog: Account, client: IgdbClient
) -> None:
    Igdb(
        [
            {"game": 1942, "uid": "1495134320", "source": GOG_SOURCE},
            {"game": 1942, "uid": "292030"},
        ],
        {1942: "The Witcher 3: Wild Hunt"},
    ).mount()
    stub = await own(session, gog, "1495134320")
    await session.commit()

    await anchor_gog(db, client)

    appids = await read(
        db,
        lambda reader: reader.scalars(
            select(ExternalId.value).where(
                ExternalId.namespace == "steam",
                ExternalId.entity_type == EntityType.WORK,
                ExternalId.entity_id == stub.id,
            )
        ),
    )
    assert list(appids) == ["292030"]


@respx.mock
async def test_a_steam_appid_is_never_asked_about_as_a_gog_product(
    db: Database, session: AsyncSession, gog: Account, client: IgdbClient
) -> None:
    igdb = Igdb([], {})
    igdb.mount()
    steam = await make_account(session)
    await own(session, steam, "292030")
    await own(session, gog, "5")
    await session.commit()

    await anchor_gog(db, client)

    assert igdb.asked_appids == ["5"]
