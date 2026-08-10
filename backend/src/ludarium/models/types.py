from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Dialect, Enum, TypeDecorator


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


def enum_column[E: StrEnum](enum_class: type[E], name: str) -> Enum:
    """`TEXT` plus a named `CHECK`, storing the value rather than the member name."""

    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
