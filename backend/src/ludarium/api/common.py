"""Lookups more than one router needs, so the answer cannot drift between them."""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ludarium.models import Provider


async def provider_or_404(session: AsyncSession, key: str) -> Provider:
    provider = await session.scalar(select(Provider).where(Provider.key == key))
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no provider named `{key}`")
    return provider
