from sqlalchemy import ForeignKey, Index, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ludarium.enums import EntityType, SourceKind
from ludarium.models.base import Base
from ludarium.models.provider import SyncRun
from ludarium.models.types import CreatedAt, JsonScalar, ScalarValue, enum_column


class FieldProvenance(Base):
    """What every source currently asserts for one field, kept side by side.

    This is the mechanism behind rules 3 and 5 rather than a record of them:
    providers write here and only the resolver writes the entity column, so the
    worst a bad sync can do is add a row that loses (rule 9).

    A snapshot, not a log. When a source changes its value the row is updated in
    place — "Steam used to call it X" is not recoverable, and nothing reads it.
    """

    __tablename__ = "field_provenance"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "field", "source_kind", "source_ref"),
        # `is_effective` is a denormalisation, so it needs a guard: without this
        # a half-finished resolve could leave two winners for one field and
        # nothing would notice.
        Index(
            "uq_field_provenance_entity_type_entity_id_field_effective",
            "entity_type",
            "entity_id",
            "field",
            unique=True,
            sqlite_where=text("is_effective"),
            postgresql_where=text("is_effective"),
        ),
        # `single_source` is a property of the registry, so the database can
        # only enforce it if the registry writes it down: `record()` sets the
        # flag, and this refuses the second source (#20, ADR-0017).
        Index(
            "uq_field_provenance_entity_type_entity_id_field_sole_source",
            "entity_type",
            "entity_id",
            "field",
            unique=True,
            sqlite_where=text("sole_source"),
            postgresql_where=text("sole_source"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Polymorphic on purpose, so no foreign key: the target is one of several
    # tables and the discriminator says which.
    entity_type: Mapped[EntityType] = mapped_column(enum_column(EntityType, "entity_type"))
    entity_id: Mapped[int]
    field: Mapped[str]
    source_kind: Mapped[SourceKind] = mapped_column(enum_column(SourceKind, "source_kind"))
    # Provider key, or `account:12` where one provider has several accounts.
    source_ref: Mapped[str]
    value: Mapped[ScalarValue | None] = mapped_column(JsonScalar())
    is_effective: Mapped[bool] = mapped_column(default=False, server_default=false())
    # True where the registry calls the field `single_source` and this row is
    # not the user's override — a denormalisation of `STRATEGIES`, kept so the
    # index above has something to be unique over. Never read; `resolver` owns
    # both the value and the decision it stands for.
    sole_source: Mapped[bool] = mapped_column(default=False, server_default=false())
    observed_at: Mapped[CreatedAt]
    # Null for user edits, which no run produced.
    run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_run.id", ondelete="RESTRICT"))

    run: Mapped[SyncRun | None] = relationship(lazy="raise_on_sql")
