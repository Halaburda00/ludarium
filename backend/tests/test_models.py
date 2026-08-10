from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.enums import ProviderKind, SourceKind, SyncStatus, SyncTrigger
from ludarium.models import Account, AppUser, Provider, SyncRun, UserSession


async def make_user(session: AsyncSession) -> AppUser:
    user = AppUser(username="owner", password_hash="not-a-hash")
    session.add(user)
    await session.flush()
    return user


async def make_provider(session: AsyncSession, key: str = "steam") -> Provider:
    provider = Provider(
        key=key,
        kind=ProviderKind.PLATFORM,
        source_kind=SourceKind.PLATFORM_API,
        display_name=key.title(),
    )
    session.add(provider)
    await session.flush()
    return provider


async def test_round_trip_with_defaults(session: AsyncSession) -> None:
    user = await make_user(session)
    provider = await make_provider(session)
    account = Account(
        provider_id=provider.id,
        external_account_id="76561197960287930",
        label="Main Steam",
        credentials_encrypted=b"fernet-ciphertext",
    )
    session.add(account)
    await session.flush()
    session.add(SyncRun(provider_id=provider.id, account_id=account.id, trigger=SyncTrigger.MANUAL))
    await session.commit()
    session.expunge_all()

    stored_user = (await session.scalars(select(AppUser))).one()
    stored_provider = (await session.scalars(select(Provider))).one()
    stored_account = (await session.scalars(select(Account))).one()
    stored_run = (await session.scalars(select(SyncRun))).one()

    assert stored_user.locale == "en"
    assert stored_user.created_at.tzinfo is not None
    assert stored_provider.enabled is True
    assert stored_provider.status is SyncStatus.PENDING
    assert stored_provider.precedence_weight == 100
    assert stored_account.user_id == user.id
    assert stored_account.is_derived is False
    assert stored_account.credentials_encrypted == b"fernet-ciphertext"
    assert stored_run.status is SyncStatus.RUNNING
    assert (stored_run.items_seen, stored_run.items_added) == (0, 0)
    assert stored_run.finished_at is None


async def test_session_round_trip(session: AsyncSession) -> None:
    user = await make_user(session)
    expires_at = datetime.now(UTC) + timedelta(days=7)
    session.add(UserSession(user_id=user.id, token_hash="sha256", expires_at=expires_at))
    await session.commit()
    session.expunge_all()

    stored = (await session.scalars(select(UserSession))).one()

    assert stored.token_hash == "sha256"
    assert stored.expires_at == expires_at


async def test_check_constraint_rejects_an_invalid_enum_value(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO provider (key, kind, source_kind, licence_class, display_name, "
                "precedence_weight, enabled, status) "
                "VALUES ('gog', 'launcher', 'platform_api', 'redistributable', 'GOG', 100, 1, "
                "'pending')"
            )
        )


async def test_user_id_defaults_to_one_outside_the_orm(session: AsyncSession) -> None:
    await make_user(session)
    provider = await make_provider(session)

    await session.execute(
        text("INSERT INTO account (provider_id, label) VALUES (:provider_id, 'raw')"),
        {"provider_id": provider.id},
    )

    assert (await session.scalars(select(Account.user_id))).one() == 1


async def test_one_account_per_external_id_and_provider(session: AsyncSession) -> None:
    await make_user(session)
    provider = await make_provider(session)
    session.add(Account(provider_id=provider.id, external_account_id="123", label="Main"))
    await session.flush()

    session.add(Account(provider_id=provider.id, external_account_id="123", label="Duplicate"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_accounts_without_an_external_id_do_not_collide(session: AsyncSession) -> None:
    await make_user(session)
    provider = await make_provider(session, key="manual")
    session.add_all(
        [
            Account(provider_id=provider.id, label="Discs"),
            Account(provider_id=provider.id, label="Unredeemed keys"),
        ]
    )

    await session.commit()

    assert len((await session.scalars(select(Account))).all()) == 2


async def test_a_provider_with_accounts_cannot_be_deleted(session: AsyncSession) -> None:
    await make_user(session)
    provider = await make_provider(session)
    session.add(Account(provider_id=provider.id, label="Main"))
    await session.flush()

    with pytest.raises(IntegrityError):
        await session.execute(text("DELETE FROM provider WHERE id = :id"), {"id": provider.id})


async def test_naive_timestamps_are_rejected(session: AsyncSession) -> None:
    user = await make_user(session)
    session.add(UserSession(user_id=user.id, token_hash="naive", expires_at=datetime(2026, 1, 1)))

    with pytest.raises(StatementError, match="naive datetime"):
        await session.flush()
