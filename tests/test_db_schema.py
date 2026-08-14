"""Schema-level guarantees.

FR-002 bans float in Python. That is only half the rule: a NUMERIC column read
into a Decimal is exact, but a DOUBLE PRECISION column silently rounds on the
way in, and the Python-side check would never see it. So the ban is asserted
against the table metadata too.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import create_engine

from arbbot.db import models  # noqa: F401  -- import registers the tables on Base.metadata
from arbbot.db.base import Base

EXPECTED_TABLES = {
    "raw_message",
    "market",
    "terms_version",
    "relationship",
    "approval",
    "audit_event",
    "book_snapshot",
    "feed_health",
    "poll_cycle",
    "venue_lease",
    "evaluation",
}


class TestSchemaShape:
    def test_expected_tables_are_registered(self) -> None:
        assert set(Base.metadata.tables) == EXPECTED_TABLES

    def test_no_column_uses_binary_floating_point(self) -> None:
        """The database half of FR-002."""
        offenders = [
            f"{table.name}.{column.name} ({column.type})"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, sa.Float)
        ]
        assert not offenders, f"float columns are prohibited: {offenders}"

    def test_timestamps_are_timezone_aware(self) -> None:
        """A market close time without a zone is not a time."""
        naive = [
            f"{table.name}.{column.name}"
            for table in Base.metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, sa.DateTime) and not column.type.timezone
        ]
        assert not naive, f"naive timestamp columns: {naive}"

    def test_constraints_are_deterministically_named(self) -> None:
        """Unnamed constraints produce migrations that differ per environment."""
        unnamed = [
            f"{table.name}: {constraint}"
            for table in Base.metadata.tables.values()
            for constraint in table.constraints
            if constraint.name is None
        ]
        assert not unnamed


def constraint_names(table_name: str) -> set[str]:
    return {str(c.name) for c in Base.metadata.tables[table_name].constraints if c.name}


class TestEvidenceIntegrity:
    def test_raw_messages_are_deduplicated_by_stream_and_sequence(self) -> None:
        """Reconnects replay messages; the archive must not double-count them.

        Keyed on the venue's own message numbering rather than on payload
        content. Content-keyed dedupe -- what M0 shipped -- silently drops two
        identical heartbeats a minute apart, both of which really happened.
        """
        assert "uq_raw_message_stream_sequence" in constraint_names("raw_message")
        assert "uq_raw_message_dedupe" not in constraint_names("raw_message")

    def test_markets_are_unique_per_venue_ticker(self) -> None:
        assert "uq_market_venue_ticker" in constraint_names("market")

    def test_relationship_versions_are_unique(self) -> None:
        assert "uq_relationship_slug_version" in constraint_names("relationship")

    def test_approval_records_the_reviewer_and_evidence(self) -> None:
        """An approval that does not say who signed and what they read is not
        an audit trail."""
        columns = Base.metadata.tables["approval"].columns
        assert not columns["reviewer"].nullable
        assert not columns["evidence"].nullable

    def test_audit_log_is_hash_chained(self) -> None:
        columns = Base.metadata.tables["audit_event"].columns
        assert "prev_hash" in columns
        assert not columns["hash"].nullable
        assert not columns["actor"].nullable


class TestSchemaCreates:
    def test_metadata_creates_cleanly(self) -> None:
        """Catches type errors that only appear at DDL emission time."""
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            found = set(sa.inspect(connection).get_table_names())
        assert found >= EXPECTED_TABLES
