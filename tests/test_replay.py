"""Deterministic replay -- FR-001's acceptance criterion.

"Raw and normalized records replay to identical book state." The test that
matters is the round trip: archive a stream exactly as it arrived, read it
back, and land on the same book. If that holds, every past decision can be
re-derived after the parser or the fee model turns out to have been wrong,
which is the whole reason the archive exists.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from arbbot.collection.archive import RawArchive
from arbbot.collection.replay import replay_archive, replay_events
from arbbot.marketdata.reconstruct import BookReconstructor
from arbbot.marketdata.types import (
    BookDelta,
    BookEvent,
    BookSide,
    DeltaEvent,
    PriceLevel,
    SnapshotEvent,
)

VENUE = "testvenue"
TICKER = "TEST-MARKET"
STREAM = f"orderbook:{TICKER}"
SCHEMA = "v1"


def decode(payload: dict[str, Any], _sequence: int | None = None) -> BookEvent | None:
    """Minimal decoder standing in for a venue adapter.

    Deliberately pure: no clock, no network, no state. That is what allows the
    archive to be replayed months later on a machine that has never held a
    credential.
    """
    kind = payload.get("kind")
    if kind == "snapshot":
        return SnapshotEvent(
            ticker=payload["ticker"],
            sequence=payload["seq"],
            levels=tuple(
                (BookSide(side), PriceLevel(Decimal(price).scaleb(-2), Decimal(qty)))
                for side, levels in payload["levels"].items()
                for price, qty in levels.items()
            ),
        )
    if kind == "delta":
        return DeltaEvent(
            ticker=payload["ticker"],
            sequence=payload["seq"],
            delta=BookDelta(
                BookSide(payload["side"]),
                Decimal(payload["price"]).scaleb(-2),
                Decimal(payload["delta"]),
            ),
        )
    return None  # heartbeats and status messages are not book events


def snapshot_payload(seq: int, yes: dict[int, int], no: dict[int, int]) -> dict[str, Any]:
    return {
        "kind": "snapshot",
        "ticker": TICKER,
        "seq": seq,
        "levels": {
            "yes": {str(p): q for p, q in yes.items()},
            "no": {str(p): q for p, q in no.items()},
        },
    }


def delta_payload(seq: int, side: str, price: int, change: int) -> dict[str, Any]:
    return {
        "kind": "delta",
        "ticker": TICKER,
        "seq": seq,
        "side": side,
        "price": price,
        "delta": change,
    }


# The heartbeat carries no sequence number, because it is not part of the
# orderbook subscription's numbering. This matters: if a non-book message
# consumed a sequence in the book's stream, skipping it during decode would
# look exactly like a dropped delta and invalidate the book on every heartbeat.
WIRE: list[dict[str, Any]] = [
    snapshot_payload(1, yes={40: 10}, no={55: 7}),
    delta_payload(2, "yes", 40, +5),
    {"kind": "heartbeat"},
    delta_payload(3, "no", 55, -2),
    delta_payload(4, "yes", 41, +3),
    delta_payload(5, "no", 52, +8),
]


def archive_wire(session: Session, payloads: list[dict[str, Any]]) -> None:
    archive = RawArchive(venue=VENUE, schema_version=SCHEMA)
    for payload in payloads:
        archive.record(
            session,
            channel="orderbook",
            payload=payload,
            subscription_key=STREAM,
            sequence=payload.get("seq"),
        )
    session.flush()


def live_checksum(payloads: list[dict[str, Any]]) -> str:
    """Book state as produced by processing the stream live."""
    reconstructor = BookReconstructor(TICKER)
    for payload in payloads:
        event = decode(payload)
        if event is not None:
            reconstructor.apply(event)
    return reconstructor.book.checksum()


class TestReplayEquivalence:
    def test_archive_replays_to_the_live_book_state(self, session: Session) -> None:
        """FR-001. The round trip that justifies keeping raw payloads at all."""
        archive_wire(session, WIRE)

        result = replay_archive(
            session, venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=decode
        )

        assert result.checksum == live_checksum(WIRE)
        assert result.is_faithful

    def test_replay_is_repeatable(self, session: Session) -> None:
        archive_wire(session, WIRE)
        kwargs = dict(venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=decode)
        first = replay_archive(session, **kwargs)  # type: ignore[arg-type]
        second = replay_archive(session, **kwargs)  # type: ignore[arg-type]
        assert first.matches(second)

    def test_non_book_messages_are_read_but_not_applied(self, session: Session) -> None:
        archive_wire(session, WIRE)
        result = replay_archive(
            session, venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=decode
        )
        assert result.messages_read == len(WIRE)
        assert result.events_decoded == len(WIRE) - 1  # the heartbeat

    def test_replay_reaches_the_expected_levels(self, session: Session) -> None:
        archive_wire(session, WIRE)
        expected = replay_events(TICKER, [e for p in WIRE if (e := decode(p)) is not None])
        actual = replay_archive(
            session, venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=decode
        )
        assert actual.matches(expected)
        assert actual.final_sequence == 5


class TestStreamIsolation:
    def test_only_the_requested_stream_is_replayed(self, session: Session) -> None:
        """Another market's messages must not leak into this book."""
        archive_wire(session, WIRE)
        other = RawArchive(venue=VENUE, schema_version=SCHEMA)
        other.record(
            session,
            channel="orderbook",
            payload=snapshot_payload(1, yes={99: 999}, no={1: 999}),
            subscription_key="orderbook:OTHER-MARKET",
            sequence=1,
        )
        session.flush()

        result = replay_archive(
            session, venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=decode
        )
        assert result.checksum == live_checksum(WIRE)

    def test_empty_stream_replays_to_an_incomplete_book(self, session: Session) -> None:
        result = replay_archive(
            session,
            venue=VENUE,
            subscription_key="orderbook:NOTHING",
            ticker=TICKER,
            decoder=decode,
        )
        assert result.messages_read == 0
        assert not result.is_complete
        assert not result.is_faithful


class TestSchemaVersionFiltering:
    def test_replay_can_be_scoped_to_one_parser_contract(self, session: Session) -> None:
        archive_wire(session, WIRE)
        newer = RawArchive(venue=VENUE, schema_version="v2")
        newer.record(
            session,
            channel="orderbook",
            payload=delta_payload(6, "yes", 40, +100),
            subscription_key=STREAM,
            sequence=6,
        )
        session.flush()

        scoped = replay_archive(
            session,
            venue=VENUE,
            subscription_key=STREAM,
            ticker=TICKER,
            decoder=decode,
            schema_version=SCHEMA,
        )
        assert scoped.checksum == live_checksum(WIRE)

        everything = replay_archive(
            session, venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=decode
        )
        assert everything.checksum != live_checksum(WIRE)


class TestDecodeFailures:
    def test_a_bad_payload_does_not_end_the_replay(self, session: Session) -> None:
        """One unparseable message must not cost the whole history."""

        def brittle(payload: dict[str, Any], _sequence: int | None = None) -> BookEvent | None:
            if payload.get("seq") == 4:
                raise ValueError("cannot parse")
            return decode(payload)

        archive_wire(session, WIRE)
        result = replay_archive(
            session, venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=brittle
        )

        assert result.decode_failures == 1
        assert result.messages_read == len(WIRE)

    def test_a_decode_failure_makes_the_replay_unfaithful(self, session: Session) -> None:
        """It completed, but it did not reproduce the archive -- and saying so
        is the difference between a replay and a guess."""

        def brittle(payload: dict[str, Any], _sequence: int | None = None) -> BookEvent | None:
            if payload.get("seq") == 4:
                raise ValueError("cannot parse")
            return decode(payload)

        archive_wire(session, WIRE)
        result = replay_archive(
            session, venue=VENUE, subscription_key=STREAM, ticker=TICKER, decoder=brittle
        )
        assert not result.is_faithful
