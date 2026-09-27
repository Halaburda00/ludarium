from typing import Any

from sqlalchemy import JSON, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ludarium.enums import MatchAction, MatchActor, MatchLayer
from ludarium.models.base import Base
from ludarium.models.types import CreatedAt, enum_column


class MatchAudit(Base):
    """Every matcher decision that moved an entitlement or a work, and enough to undo it.

    No foreign keys: the trail has to outlive what it names. A `merged` row's
    `previous_work_id` is a work the merge itself deleted, and its `work_id` may
    be merged away in turn.
    """

    __tablename__ = "match_audit"
    __table_args__ = (Index("ix_match_audit_work_id", "work_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Null for `merged`, which is work-to-work and touches many entitlements.
    entitlement_id: Mapped[int | None]
    # The surviving work for `merged`, the restored one for `unmerged`.
    work_id: Mapped[int]
    action: Mapped[MatchAction] = mapped_column(enum_column(MatchAction, "match_action"))
    layer: Mapped[MatchLayer | None] = mapped_column(enum_column(MatchLayer, "match_layer"))
    # The deleted source for `merged`, the work it was folded into for `unmerged`.
    previous_work_id: Mapped[int | None]
    # The undo payload for `merged`; `{"undoes": id}` for `unmerged`.
    details: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    actor: Mapped[MatchActor] = mapped_column(enum_column(MatchActor, "match_actor"))
    created_at: Mapped[CreatedAt]
