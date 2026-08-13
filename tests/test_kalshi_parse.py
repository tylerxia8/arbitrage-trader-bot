"""Kalshi wire-format decoding.

Fixtures are real payloads captured from the production API on 2026-08-12,
plus the documented WebSocket message shapes. Both matter: the docs give the
message envelope, and the live capture proves what the values actually look
like -- which is how the integer-cents model this milestone started with was
found to be wrong.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from arbbot.marketdata.types import BookSide, DeltaEvent, MarketStatus, SnapshotEvent
from arbbot.venues.kalshi import KalshiAdapter, KalshiParseError
from arbbot.venues.kalshi.parse import decode_book_event, decode_market, decode_rest_orderbook

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"
D = Decimal


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


# Documented shape, from docs.kalshi.com/websockets/orderbook-updates.
WS_SNAPSHOT: dict[str, Any] = {
    "type": "orderbook_snapshot",
    "sid": 2,
    "seq": 2,
    "msg": {
        "market_ticker": "FED-23DEC-T3.00",
        "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
        "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
    },
}

WS_DELTA: dict[str, Any] = {
    "type": "orderbook_delta",
    "sid": 2,
    "seq": 3,
    "msg": {
        "market_ticker": "FED-23DEC-T3.00",
        "price_dollars": "0.9600",
        "delta_fp": "-54.00",
        "side": "yes",
        "ts_ms": 1669149841000,
    },
}


class TestSnapshotDecoding:
    def test_decodes_both_sides(self) -> None:
        event = decode_book_event(WS_SNAPSHOT)
        assert isinstance(event, SnapshotEvent)
        assert event.ticker == "FED-23DEC-T3.00"
        assert event.sequence == 2
        assert len(event.levels) == 4

    def test_prices_and_sizes_are_exact_decimals(self) -> None:
        event = decode_book_event(WS_SNAPSHOT)
        assert isinstance(event, SnapshotEvent)
        yes = [level for side, level in event.levels if side is BookSide.YES]
        assert yes[0].price_dollars == D("0.0800")
        assert yes[0].quantity == D("300.00")

    def test_accepts_the_rest_key_spelling_too(self) -> None:
        """The WS snapshot suffixes both keys with _fp; REST does not. A single
        unrecognised key would produce a silently empty book, so both spellings
        are accepted rather than assumed."""
        payload = {
            "type": "orderbook_snapshot",
            "seq": 5,
            "msg": {
                "market_ticker": "X",
                "yes_dollars": [["0.1000", "5.00"]],
                "no_dollars": [["0.8000", "6.00"]],
            },
        }
        event = decode_book_event(payload)
        assert isinstance(event, SnapshotEvent)
        assert len(event.levels) == 2

    def test_missing_sides_decode_to_an_empty_book(self) -> None:
        payload = {"type": "orderbook_snapshot", "seq": 1, "msg": {"market_ticker": "X"}}
        event = decode_book_event(payload)
        assert isinstance(event, SnapshotEvent)
        assert event.levels == ()


class TestDeltaDecoding:
    def test_decodes_a_negative_delta(self) -> None:
        event = decode_book_event(WS_DELTA)
        assert isinstance(event, DeltaEvent)
        assert event.sequence == 3
        assert event.delta.side is BookSide.YES
        assert event.delta.price_dollars == D("0.9600")
        assert event.delta.delta == D("-54.00")

    def test_decodes_a_positive_delta(self) -> None:
        payload = {**WS_DELTA, "msg": {**WS_DELTA["msg"], "delta_fp": "12.50"}}
        event = decode_book_event(payload)
        assert isinstance(event, DeltaEvent)
        assert event.delta.delta == D("12.50")

    def test_fractional_deltas_survive(self) -> None:
        """Minimum granularity is 0.01 contracts, not 1."""
        payload = {**WS_DELTA, "msg": {**WS_DELTA["msg"], "delta_fp": "-0.25"}}
        event = decode_book_event(payload)
        assert isinstance(event, DeltaEvent)
        assert event.delta.delta == D("-0.25")

    def test_unknown_side_is_rejected(self) -> None:
        payload = {**WS_DELTA, "msg": {**WS_DELTA["msg"], "side": "maybe"}}
        with pytest.raises(KalshiParseError, match="unknown side"):
            decode_book_event(payload)

    def test_missing_field_is_rejected(self) -> None:
        msg = {k: v for k, v in WS_DELTA["msg"].items() if k != "delta_fp"}
        with pytest.raises(KalshiParseError, match="delta_fp"):
            decode_book_event({**WS_DELTA, "msg": msg})


class TestNonBookMessages:
    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 2}},
            {"type": "ok", "id": 1},
            {"type": "ticker", "seq": 4, "msg": {}},
        ],
    )
    def test_unrelated_types_decode_to_none(self, payload: dict[str, Any]) -> None:
        assert decode_book_event(payload) is None


class TestMalformedBookMessages:
    def test_a_book_message_without_a_sequence_is_rejected(self) -> None:
        """Silently skipping it would leave a hole in a stream that reports
        itself as intact."""
        payload = {"type": "orderbook_delta", "msg": WS_DELTA["msg"]}
        with pytest.raises(KalshiParseError, match="seq"):
            decode_book_event(payload)

    def test_a_book_message_without_a_ticker_is_rejected(self) -> None:
        with pytest.raises(KalshiParseError, match="market_ticker"):
            decode_book_event({"type": "orderbook_snapshot", "seq": 1, "msg": {}})

    def test_a_numeric_price_is_rejected(self) -> None:
        """The venue sends strings precisely so precision survives; a JSON
        number has already been through a float by the time we see it."""
        payload = {
            "type": "orderbook_snapshot",
            "seq": 1,
            "msg": {"market_ticker": "X", "yes_dollars": [[0.08, "300.00"]]},
        }
        with pytest.raises(KalshiParseError):
            decode_book_event(payload)

    def test_a_malformed_level_is_rejected(self) -> None:
        payload = {
            "type": "orderbook_snapshot",
            "seq": 1,
            "msg": {"market_ticker": "X", "yes_dollars": [["0.08"]]},
        }
        with pytest.raises(KalshiParseError, match="malformed price level"):
            decode_book_event(payload)


class TestLiveRestFixtures:
    def test_decodes_the_captured_orderbook(self) -> None:
        event = decode_rest_orderbook("KXNFLGAME-26AUG15DALSEA-SEA", load("orderbook_rest.json"), 1)
        assert len(event.levels) == 8

    def test_captured_quantities_are_fractional(self) -> None:
        """This is the observation that invalidated the integer-size model."""
        event = decode_rest_orderbook("KXNFLGAME-26AUG15DALSEA-SEA", load("orderbook_rest.json"), 1)
        quantities = {level.quantity for _, level in event.levels}
        assert any(q % 1 != 0 for q in quantities), "expected a fractional contract count"

    def test_derived_ask_matches_the_venues_quoted_ask(self) -> None:
        """End-to-end against reality: decode the captured book, derive the
        YES ask, and check it equals the yes_ask_dollars the venue published
        for the same market at the same moment."""
        from arbbot.marketdata.book import OrderBook

        event = decode_rest_orderbook("KXNFLGAME-26AUG15DALSEA-SEA", load("orderbook_rest.json"), 1)
        book = OrderBook("KXNFLGAME-26AUG15DALSEA-SEA")
        book.apply_snapshot(event.levels, sequence=1)

        market = load("market_rest.json")["market"]
        yes_ask = book.best_ask(BookSide.YES)
        assert yes_ask is not None
        assert yes_ask.price_dollars == D(market["yes_ask_dollars"])


class TestMarketDecoding:
    def test_decodes_the_captured_market(self) -> None:
        record = decode_market(load("market_rest.json")["market"])
        assert record.ticker == "KXNFLGAME-26AUG15DALSEA-SEA"
        assert record.event_id == "KXNFLGAME-26AUG15DALSEA"
        assert record.status is MarketStatus.OPEN

    def test_timestamps_are_timezone_aware(self) -> None:
        record = decode_market(load("market_rest.json")["market"])
        assert record.close_ts is not None
        assert record.close_ts.tzinfo is not None

    def test_active_maps_to_open(self) -> None:
        """The venue says "active"; the normalized vocabulary says OPEN. A
        mapping miss here would make every market look untradeable."""
        assert decode_market({"ticker": "X", "status": "active"}).status is MarketStatus.OPEN

    def test_unrecognised_status_is_not_tradeable(self) -> None:
        """An unknown status is not evidence that trading is safe."""
        record = decode_market({"ticker": "X", "status": "some_new_state"})
        assert record.status is MarketStatus.UNKNOWN
        assert not record.status.is_tradeable

    def test_a_market_without_a_ticker_is_rejected(self) -> None:
        with pytest.raises(KalshiParseError, match="ticker"):
            decode_market({"status": "active"})


class TestAdapterConformance:
    def test_adapter_satisfies_the_venue_protocol(self) -> None:
        from arbbot.venues.base import VenueAdapter

        assert isinstance(KalshiAdapter(), VenueAdapter)

    def test_subscription_keys_separate_markets(self) -> None:
        """Kalshi reuses one sid across every market in a channel
        subscription, so the sid alone cannot scope a sequence number."""
        adapter = KalshiAdapter()
        assert adapter.subscription_key("orderbook_delta", "AAA") != adapter.subscription_key(
            "orderbook_delta", "BBB"
        )

    def test_schema_version_is_recorded(self) -> None:
        assert KalshiAdapter().schema_version == "kalshi-fp-v1"

    def test_decoding_does_not_mutate_the_payload(self) -> None:
        """The archive row must stay exactly what the venue sent."""
        payload = json.loads(json.dumps(WS_DELTA))
        KalshiAdapter().decode_book_event(payload)
        assert payload == WS_DELTA
