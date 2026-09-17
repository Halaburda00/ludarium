from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ludarium.models.base import Base

type Payload = dict[str, Any] | list[Any]


class FetchCache(Base):
    """What a provider answered when enrichment asked it something, and when (ADR-0019).

    A cache, not a source: the step that asked reads it, and records provenance
    from the answer like any provider (rule 9). Emptying the table loses nothing
    but the requests it would take to fill it again.

    Most of it is `runtime_only` data — IGDB and RAWG may not be redistributed —
    so it lives in the database under the data directory and is left out of
    every export whole, whatever the provider's `licence_class`.
    """

    __tablename__ = "fetch_cache"
    __table_args__ = (
        UniqueConstraint("provider_id", "resource", "key"),
        # Finding what has gone stale is a range over `fetched_at` within one
        # resource, and the unique constraint above cannot serve that order.
        Index(
            "ix_fetch_cache_provider_id_resource_fetched_at",
            "provider_id",
            "resource",
            "fetched_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("provider.id", ondelete="RESTRICT"))
    # What was asked, in the provider's own terms: `games`, `external_games/steam`.
    resource: Mapped[str]
    # Which one: an IGDB id, a Steam appid. Text, because the identifiers are.
    key: Mapped[str]
    # SQL null means the provider was asked and has nothing under this key, which
    # is worth keeping: a playtest IGDB has never heard of would otherwise be
    # asked about again on every run.
    payload: Mapped[Payload | None] = mapped_column(
        JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
    )
    fetched_at: Mapped[datetime]
