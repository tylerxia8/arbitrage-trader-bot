"""Kalshi wire-format decoding.

Verified against live documentation and the production API on 2026-08-12.
Everything here is pure: no clock, no network, no state. That is what lets an
archived payload be replayed months later, under a different parser version,
on a machine that has never held a credential.

Two properties of the wire format drive the design.

**Money arrives as strings.** ``*_dollars`` fields are decimal strings with up
to four decimal places, and ``*_fp`` fields are contract counts with up to two.
The venue encodes them as strings so they survive transport exactly; parsing
them through ``float`` -- the default behaviour of most JSON tooling -- throws
that away. They are parsed with :mod:`arbbot.money`, which refuses floats.

**Tick size varies per market.** ``price_level_structure`` is ``linear_cent``
($0.01 steps) on some markets and ``deci_cent`` ($0.001) on others. Assuming
whole cents would silently truncate a real quote.

If the wire format changes, bump :data:`SCHEMA_VERSION`. Archived payloads
remain interpretable under the parser that captured them (ADR-0005).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

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
from arbbot.money import MoneyError, parse_quantity, parse_venue_dollars

__all__ = [
    "SCHEMA_VERSION",
    "VENUE",
    "KalshiParseError",
    "decode_book_event",
    "decode_market",
    "decode_rest_orderbook",
]

VENUE: Final = "kalshi"

#: Parser contract version. Bump on any change to how a payload is interpreted.
#: v1 corresponds to the post-fixed-point-migration wire format: ``*_dollars``
#: price strings and ``*_fp`` contract counts.
SCHEMA_VERSION: Final = "kalshi-fp-v1"

#: WebSocket message types this parser understands.
_SNAPSHOT_TYPE: Final = "orderbook_snapshot"
_DELTA_TYPE: Final = "orderbook_delta"

#: Venue status strings mapped to the normalized lifecycle. Anything absent
#: maps to UNKNOWN, which is deliberately not tradeable: an unrecognised
#: status is not evidence that trading is safe.
_STATUS_MAP: Final[dict[str, MarketStatus]] = {
    "unopened": MarketStatus.UNOPENED,
    "initialized": MarketStatus.UNOPENED,
    "active": MarketStatus.OPEN,
    "open": MarketStatus.OPEN,
    "paused": MarketStatus.PAUSED,
    "closed": MarketStatus.CLOSED,
    "finalized": MarketStatus.SETTLED,
    "settled": MarketStatus.SETTLED,
    "determined": MarketStatus.SETTLED,
}


class KalshiParseError(ValueError):
    """Raised when a payload should have decoded and did not."""


def _levels(raw: Any, side: BookSide) -> list[tuple[BookSide, PriceLevel]]:
    """Parse one side's ``[[price, count], ...]`` array.

    The venue sends these ascending, best bid last. Order is not relied on --
    the book stores levels in a mapping and derives ordering itself, so a
    change in the venue's sort cannot silently reorder our book.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise KalshiParseError(f"{side.value} levels must be a list, got {type(raw).__name__}")

    parsed: list[tuple[BookSide, PriceLevel]] = []
    for entry in raw:
        if not isinstance(entry, list | tuple) or len(entry) != 2:
            raise KalshiParseError(f"malformed price level: {entry!r}")
        price_raw, count_raw = entry
        try:
            level = PriceLevel(
                price_dollars=parse_venue_dollars(price_raw),
                quantity=parse_quantity(count_raw),
            )
        except MoneyError as exc:
            raise KalshiParseError(f"bad {side.value} level {entry!r}: {exc}") from exc
        parsed.append((side, level))
    return parsed


def decode_book_event(payload: dict[str, Any]) -> BookEvent | None:
    """Decode a WebSocket payload into a book event.

    Returns ``None`` for messages that are not book updates -- heartbeats,
    subscription acknowledgements, status notices. Raises for messages that
    claim to be book updates and cannot be understood, because silently
    skipping one would leave a gap in a stream that reports itself as intact.
    """
    message_type = payload.get("type")
    if message_type not in (_SNAPSHOT_TYPE, _DELTA_TYPE):
        return None

    sequence = payload.get("seq")
    if not isinstance(sequence, int):
        raise KalshiParseError(f"{message_type} has no usable seq: {sequence!r}")

    body = payload.get("msg")
    if not isinstance(body, dict):
        raise KalshiParseError(f"{message_type} has no msg object")

    ticker = body.get("market_ticker")
    if not isinstance(ticker, str) or not ticker:
        raise KalshiParseError(f"{message_type} has no market_ticker")

    if message_type == _SNAPSHOT_TYPE:
        # The snapshot channel suffixes both keys with _fp; the REST endpoint
        # does not. Accept either rather than assuming, since a single
        # unrecognised key would produce a silently empty book.
        yes = body.get("yes_dollars_fp", body.get("yes_dollars"))
        no = body.get("no_dollars_fp", body.get("no_dollars"))
        return SnapshotEvent(
            ticker=ticker,
            sequence=sequence,
            levels=tuple(_levels(yes, BookSide.YES) + _levels(no, BookSide.NO)),
        )

    side_raw = body.get("side")
    if side_raw not in ("yes", "no"):
        raise KalshiParseError(f"orderbook_delta has unknown side {side_raw!r}")

    try:
        price = parse_venue_dollars(body["price_dollars"])
        # delta_fp is signed: parse_quantity rejects negatives, so the sign is
        # stripped and reapplied rather than special-casing the parser.
        raw_delta = body["delta_fp"]
        if not isinstance(raw_delta, str):
            raise KalshiParseError(f"delta_fp must be a string, got {type(raw_delta).__name__}")
        negative = raw_delta.lstrip().startswith("-")
        magnitude = parse_quantity(raw_delta.lstrip().lstrip("+-"))
        delta = -magnitude if negative else magnitude
    except KeyError as exc:
        raise KalshiParseError(f"orderbook_delta missing {exc.args[0]}") from exc
    except MoneyError as exc:
        raise KalshiParseError(f"bad orderbook_delta values: {exc}") from exc

    return DeltaEvent(
        ticker=ticker,
        sequence=sequence,
        delta=BookDelta(side=BookSide(side_raw), price_dollars=price, delta=delta),
    )


def decode_rest_orderbook(ticker: str, payload: dict[str, Any], sequence: int) -> SnapshotEvent:
    """Decode a ``GET /markets/{ticker}/orderbook`` response.

    REST has no sequence number of its own, so the caller supplies one. A REST
    snapshot is a starting point for a stream, not a participant in it.
    """
    book = payload.get("orderbook_fp")
    if not isinstance(book, dict):
        raise KalshiParseError("orderbook response has no orderbook_fp object")

    return SnapshotEvent(
        ticker=ticker,
        sequence=sequence,
        levels=tuple(
            _levels(book.get("yes_dollars"), BookSide.YES)
            + _levels(book.get("no_dollars"), BookSide.NO)
        ),
    )


def _timestamp(value: Any) -> dt.datetime | None:
    """Parse an RFC3339 timestamp, always timezone-aware."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def decode_market(payload: dict[str, Any]) -> MarketSnapshotRecord:
    """Decode market metadata from ``GET /markets``."""
    ticker = payload.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        raise KalshiParseError("market payload has no ticker")

    status_raw = payload.get("status")
    status = (
        _STATUS_MAP.get(status_raw, MarketStatus.UNKNOWN)
        if isinstance(status_raw, str)
        else MarketStatus.UNKNOWN
    )

    return MarketSnapshotRecord(
        venue=VENUE,
        ticker=ticker,
        event_id=str(payload.get("event_ticker", "")),
        title=str(payload.get("title", "")),
        status=status,
        close_ts=_timestamp(payload.get("close_time")),
        settlement_ts=_timestamp(payload.get("expiration_time")),
    )
