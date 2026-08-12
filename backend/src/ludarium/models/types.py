import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from sqlalchemy import DateTime, Dialect, Enum, Text, TypeDecorator, func
from sqlalchemy.orm import mapped_column

type ScalarValue = bool | float | int | str


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC in Python, whatever the dialect stores.

    SQLite has no timezone type and silently drops the offset, so a naive value
    written once is indistinguishable from a UTC one later. Rejecting naive
    input on the way in is what keeps "UTC, no local time anywhere" true.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; use datetime.now(UTC)")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class JsonScalar(TypeDecorator[ScalarValue]):
    """One JSON-encoded scalar in a `TEXT` column.

    The encoding is the column's job, not the caller's. Left to a caller it
    eventually gets `str(value)` — `"True"` where `true` was meant — and the
    damage shows up at the first `json.loads` in the resolver, or never, as a
    precedence comparison that quietly stops matching (rule 5).

    A null column means "this source explicitly has no value", which is not the
    same as having no row, so `None` stays SQL `NULL` rather than JSON `null`.
    """

    impl = Text()
    cache_ok = True

    def process_bind_param(self, value: ScalarValue | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, bool | float | int | str):
            raise ValueError(f"provenance holds one scalar, not {type(value).__name__}")
        return json.dumps(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> ScalarValue | None:
        if value is None:
            return None
        decoded: ScalarValue = json.loads(value)
        return decoded


# Annotated rather than repeated: a dozen columns want exactly this, and the
# one written out by hand is the one that loses `onupdate`.
CreatedAt = Annotated[datetime, mapped_column(default=utcnow, server_default=func.now())]
UpdatedAt = Annotated[
    datetime, mapped_column(default=utcnow, onupdate=utcnow, server_default=func.now())
]


def enum_column[E: StrEnum](enum_class: type[E], name: str) -> Enum:
    """`TEXT` plus a named `CHECK`, storing the value rather than the member name."""

    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )
