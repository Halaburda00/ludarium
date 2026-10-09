"""A derived account's ids are its report's, never treated as the platform's (ADR-0039)."""

from conftest import make_account, make_work
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium import classification, matching, reviews
from ludarium.db import Database
from ludarium.enums import ItemKind, WorkLinkRole
from ludarium.models import Account, Entitlement, EntitlementWork, Work


async def own(session: AsyncSession, account: Account, item_id: str, title: str) -> Work:
    work = await make_work(session, title)
    work.item_kind = ItemKind.GAME
    entitlement = Entitlement(account_id=account.id, provider_item_id=item_id, provider_title=title)
    session.add(entitlement)
    await session.flush()
    session.add(
        EntitlementWork(entitlement_id=entitlement.id, work_id=work.id, role=WorkLinkRole.PRIMARY)
    )
    await session.flush()
    return work


async def library(session: AsyncSession, key: str) -> tuple[Work, Work]:
    """A copy on a connected account and one on a derived account, both on `key`."""

    connected = await make_account(session, key)
    derived = await make_account(session, key, external_account_id="import:steam")
    derived.is_derived = True
    synced = await own(session, connected, "620", "Portal 2")
    # Numeric on purpose: the guard is the account, not the id's shape.
    reported = await own(session, derived, "5", "A row from a spreadsheet")
    await session.commit()
    return synced, reported


async def test_only_a_synced_appid_is_classified(db: Database, session: AsyncSession) -> None:
    synced, _ = await library(session, "steam")

    assert await classification._library(db) == [("620", synced.id)]


async def test_only_a_synced_appid_is_asked_for_reviews(
    db: Database, session: AsyncSession
) -> None:
    synced, _ = await library(session, "steam")

    assert await reviews._library(db) == {synced.id: frozenset({"620"})}


async def test_only_a_synced_id_is_anchored_by_layer_one(
    db: Database, session: AsyncSession
) -> None:
    synced, _ = await library(session, "gog")

    assert await matching._unmatched_games(db, "gog") == [("620", synced.id)]


async def test_an_epic_copy_on_a_derived_account_is_not_matched(
    db: Database, session: AsyncSession
) -> None:
    _, reported = await library(session, "epic")
    entitlement = await session.get(Entitlement, 2)
    assert entitlement is not None
    # The shape Epic's own copies carry, so only the account can turn it away.
    entitlement.raw_payload = {"namespace": "0123456789abcdef0123456789abcdef"}
    await session.commit()

    assert reported.id not in {work for _, work, _ in await matching._unmatched_epic_games(db)}
