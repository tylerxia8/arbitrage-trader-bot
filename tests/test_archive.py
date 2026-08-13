"""The raw archive.

The deduplication tests encode a correction made during M1. Keying dedupe on
payload content looks obviously right and quietly loses data: two heartbeats a
minute apart are byte-identical and both real. Identity is the venue's own
statement of "message N of this subscription", so that is what the archive
keys on.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arbbot.collection.archive import RawArchive, canonical_hash
from arbbot.db.models import RawMessage

VENUE = "testvenue"
SCHEMA = "v1"


@pytest.fixture
def archive() -> RawArchive:
    return RawArchive(venue=VENUE, schema_version=SCHEMA)


def count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(RawMessage)).scalar_one()


class TestCanonicalHash:
    def test_key_order_does_not_change_the_hash(self) -> None:
        """The fingerprint must depend on content, not on how the venue's
        serialiser happened to order a dictionary."""
        assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})

    def test_different_content_hashes_differently(self) -> None:
        assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})

    def test_hash_is_hex_sha256(self) -> None:
        digest = canonical_hash({"a": 1})
        assert len(digest) == 64
        assert int(digest, 16) >= 0


class TestRecording:
    def test_stores_the_payload_verbatim(self, session: Session, archive: RawArchive) -> None:
        payload = {"type": "delta", "price": 42, "delta": -3}
        archive.record(
            session, channel="orderbook", payload=payload, subscription_key="ob:X", sequence=1
        )
        session.flush()

        row = session.execute(select(RawMessage)).scalar_one()
        assert row.payload == payload
        assert row.sha256 == canonical_hash(payload)
        assert row.schema_version == SCHEMA

    def test_records_the_capture_time_parser_version(
        self, session: Session, archive: RawArchive
    ) -> None:
        """A later parser change must not be able to reinterpret old payloads
        without that being visible."""
        archive.record(session, channel="c", payload={"a": 1}, subscription_key="s", sequence=1)
        session.flush()
        assert session.execute(select(RawMessage.schema_version)).scalar_one() == SCHEMA

    def test_preserves_source_timestamp(self, session: Session, archive: RawArchive) -> None:
        source = dt.datetime(2026, 8, 12, 10, 30, tzinfo=dt.UTC)
        archive.record(
            session,
            channel="c",
            payload={"a": 1},
            subscription_key="s",
            sequence=1,
            source_ts=source,
        )
        session.flush()

        stored = session.execute(select(RawMessage.source_ts)).scalar_one()
        assert stored is not None
        # SQLite has no timezone-aware column type and hands back a naive value;
        # PostgreSQL round-trips the offset. Both mean UTC here, because the
        # value written was UTC -- so normalise before comparing rather than
        # asserting a representation detail of the test database.
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=dt.UTC)
        assert stored == source


class TestDeduplication:
    def test_same_stream_and_sequence_is_a_duplicate(
        self, session: Session, archive: RawArchive
    ) -> None:
        first = archive.record(
            session, channel="c", payload={"n": 1}, subscription_key="ob:X", sequence=7
        )
        session.flush()
        second = archive.record(
            session, channel="c", payload={"n": 1}, subscription_key="ob:X", sequence=7
        )

        assert first.stored
        assert not first.was_duplicate
        assert second.was_duplicate
        assert not second.stored
        assert count(session) == 1

    def test_identical_payloads_on_different_sequences_are_both_kept(
        self, session: Session, archive: RawArchive
    ) -> None:
        """The M0 bug: content-keyed dedupe would have dropped the second."""
        archive.record(
            session, channel="c", payload={"tick": True}, subscription_key="s", sequence=1
        )
        archive.record(
            session, channel="c", payload={"tick": True}, subscription_key="s", sequence=2
        )
        session.flush()
        assert count(session) == 2

    def test_same_sequence_on_different_markets_are_both_kept(
        self, session: Session, archive: RawArchive
    ) -> None:
        """Sequence numbers are per-subscription: every market has its own
        message 1."""
        archive.record(
            session, channel="ob", payload={"n": 1}, subscription_key="ob:AAA", sequence=1
        )
        archive.record(
            session, channel="ob", payload={"n": 1}, subscription_key="ob:BBB", sequence=1
        )
        session.flush()
        assert count(session) == 2

    def test_unsequenced_messages_are_always_stored(
        self, session: Session, archive: RawArchive
    ) -> None:
        """Two heartbeats a minute apart are identical and both real."""
        for _ in range(3):
            archive.record(session, channel="heartbeat", payload={"hb": True})
        session.flush()
        assert count(session) == 3

    def test_a_sequenced_message_requires_a_stream_identity(
        self, session: Session, archive: RawArchive
    ) -> None:
        """Without it, one market's sequence 5 collides with another's."""
        with pytest.raises(ValueError, match="subscription_key"):
            archive.record(session, channel="c", payload={"a": 1}, sequence=5)
