"""Declarative base and shared column types.

The naming convention matters: without it, Alembic autogenerate produces
migrations with database-assigned constraint names that differ between
environments, and a later ``DROP CONSTRAINT`` silently targets nothing. Naming
every constraint deterministically keeps migrations reproducible, which is a
precondition for the replay guarantee (NFR-03).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, MetaData, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column, registry

__all__ = ["Base", "BigIntPk", "Json", "JsonList", "Money", "Sha256", "Timestamp"]

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Timezone-aware timestamp. Naive datetimes are banned project-wide (ruff DTZ)
#: because a market close time is meaningless without its zone.
Timestamp = Annotated[dt.datetime, mapped_column(DateTime(timezone=True))]

#: Exact money. NUMERIC(18, 8) holds sub-cent fee arithmetic without loss.
#: DOUBLE PRECISION is prohibited here for the same reason float is prohibited
#: in Python -- see :mod:`arbbot.money`.
Money = Annotated[Decimal, mapped_column(Numeric(18, 8))]

#: Hex-encoded SHA-256 of an immutable payload or a normalized terms document.
Sha256 = Annotated[str, mapped_column(String(64))]

#: JSONB on PostgreSQL, plain JSON on SQLite fixtures.
Json = Annotated[dict[str, Any], mapped_column(JSON().with_variant(JSONB, "postgresql"))]

#: The same column type for a JSON *array*. Separate from :data:`Json` so the
#: type checker knows the difference: a list column annotated as a dict silently
#: makes every membership and equality check against it a no-op.
JsonList = Annotated[list[str], mapped_column(JSON().with_variant(JSONB, "postgresql"))]

#: Auto-incrementing 64-bit surrogate key for high-volume append-only tables.
#:
#: The SQLite variant is load-bearing rather than cosmetic. SQLite only
#: auto-populates a rowid alias declared exactly ``INTEGER PRIMARY KEY``; a
#: ``BIGINT`` primary key is an ordinary column there, so every insert in the
#: local test suite fails on a NOT NULL violation. PostgreSQL, where the
#: archive actually lives, still gets BIGINT -- a 32-bit key would be a real
#: ceiling for a table that stores every message the venue ever sent.
BigIntPk = Annotated[
    int,
    mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    ),
]


class Base(DeclarativeBase):
    """Declarative base for every table in the system."""

    registry = registry(
        type_annotation_map={
            dt.datetime: DateTime(timezone=True),
            Decimal: Numeric(18, 8),
        }
    )
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
