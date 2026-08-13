"""The venue adapter boundary.

No venue is implemented yet -- the Kalshi wire format has to be verified
against live documentation before anything decodes it for real. What can be
checked now is that the interface is implementable and that a conforming
adapter is genuinely pure, because purity is what allows the archive to be
replayed months later, with a different parser, on a machine that has never
held a credential.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from arbbot.marketdata.types import (
    BookDelta,
    BookEvent,
    BookSide,
    DeltaEvent,
    MarketSnapshotRecord,
    MarketStatus,
    PriceLevel,
    SnapshotEvent,
)
from arbbot.venues.base import VenueAdapter


class FakeAdapter:
    """A minimal conforming adapter, standing in for a real venue."""

    @property
    def venue(self) -> str:
        return "fakevenue"

    @property
    def schema_version(self) -> str:
        return "v1"

    def subscription_key(self, channel: str, ticker: str) -> str:
        return f"{channel}:{ticker}"

    def decode_book_event(self, payload: dict[str, Any]) -> BookEvent | None:
        if payload.get("kind") == "snapshot":
            return SnapshotEvent(
                ticker=payload["ticker"],
                sequence=payload["seq"],
                levels=((BookSide.YES, PriceLevel(Decimal("0.40"), Decimal("10"))),),
            )
        if payload.get("kind") == "delta":
            return DeltaEvent(
                ticker=payload["ticker"],
                sequence=payload["seq"],
                delta=BookDelta(BookSide.YES, Decimal("0.40"), Decimal("1")),
            )
        return None

    def decode_market(self, payload: dict[str, Any]) -> MarketSnapshotRecord:
        return MarketSnapshotRecord(
            venue=self.venue,
            ticker=payload["ticker"],
            event_id=payload["event_id"],
            title=payload["title"],
            status=MarketStatus.OPEN,
            close_ts=None,
            settlement_ts=None,
        )


class TestConformance:
    def test_a_plain_class_satisfies_the_protocol(self) -> None:
        assert isinstance(FakeAdapter(), VenueAdapter)

    def test_subscription_keys_separate_markets(self) -> None:
        """Sequence numbers are per-subscription; every market has its own
        message 1, and conflating them corrupts both books."""
        adapter = FakeAdapter()
        assert adapter.subscription_key("orderbook", "AAA") != adapter.subscription_key(
            "orderbook", "BBB"
        )


class TestDecoderPurity:
    def test_decoding_is_repeatable(self) -> None:
        """Same payload, same event, every time -- no clock, no counter, no
        state. Replay depends on this holding months after capture."""
        adapter = FakeAdapter()
        payload = {"kind": "snapshot", "ticker": "AAA", "seq": 1}
        assert adapter.decode_book_event(payload) == adapter.decode_book_event(payload)

    def test_decoder_does_not_mutate_the_payload(self) -> None:
        """The archive row must remain exactly what the venue sent."""
        adapter = FakeAdapter()
        payload = {"kind": "delta", "ticker": "AAA", "seq": 2}
        original = dict(payload)
        adapter.decode_book_event(payload)
        assert payload == original

    def test_unrelated_payloads_decode_to_nothing(self) -> None:
        assert FakeAdapter().decode_book_event({"kind": "heartbeat"}) is None


class TestMarketDecoding:
    def test_unknown_status_is_not_tradeable(self) -> None:
        """An unrecognised venue status is not evidence that trading is safe."""
        assert not MarketStatus.UNKNOWN.is_tradeable

    def test_only_open_is_tradeable(self) -> None:
        tradeable = {s for s in MarketStatus if s.is_tradeable}
        assert tradeable == {MarketStatus.OPEN}
