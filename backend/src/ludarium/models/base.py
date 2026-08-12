from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import Boolean, Date, Float, Integer, LargeBinary, MetaData, Text
from sqlalchemy.orm import DeclarativeBase

from ludarium.models.types import UtcDateTime

# Named constraints are what makes an Alembic migration on SQLite possible at
# all: batch mode recreates a table and has to name what it recreates.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        bool: Boolean(),
        bytes: LargeBinary(),
        # `date` is listed next to `datetime` on purpose: `datetime` subclasses
        # it, and only an exact entry keeps a release date out of UtcDateTime.
        date: Date(),
        datetime: UtcDateTime(),
        float: Float(),
        int: Integer(),
        str: Text(),
    }
