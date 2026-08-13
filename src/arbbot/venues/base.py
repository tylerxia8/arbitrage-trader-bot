"""Venue adapter interface.

Everything venue-specific lives behind this boundary: endpoint shapes, field
names, sequence semantics, fee rules. Everything above it speaks only the
normalized vocabulary in :mod:`arbbot.marketdata.types`.

The interface exists now, before any adapter implements it, because it
constrains what a venue is allowed to be. In particular ``decode_book_event``
is a pure function from payload to event — no network, no clock, no state.
That is what lets the archive be replayed through the same decoder that
processed it live, months later, on a machine that has never held a
credential.

Adapters must carry a ``schema_version``. When a venue changes its wire
format, the version changes with it, and past payloads remain interpretable
under the parser that captured them (ADR-0005).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from arbbot.marketdata.types import BookEvent, MarketSnapshotRecord

__all__ = ["MarketDataSource", "VenueAdapter"]


@runtime_checkable
class VenueAdapter(Protocol):
    """Static, offline capabilities of a venue integration."""

    @property
    def venue(self) -> str:
        """Short venue identifier, stored on every archived message."""
        ...

    @property
    def schema_version(self) -> str:
        """Parser contract version. Changes whenever decoding changes meaning."""
        ...

    def subscription_key(self, channel: str, ticker: str) -> str:
        """Identity of one stream, used to scope sequence numbers.

        Sequence numbers are per-subscription, so this must distinguish
        markets: two markets on the same channel each start at their own
        message 1.
        """
        ...

    def decode_book_event(self, payload: dict[str, Any]) -> BookEvent | None:
        """Decode an archived payload into a book event.

        Pure: no I/O, no clock, no mutable state. Returns ``None`` for
        payloads that are not book messages (heartbeats, status updates).
        Raises for payloads that should have decoded and did not.
        """
        ...

    def decode_market(self, payload: dict[str, Any]) -> MarketSnapshotRecord:
        """Decode market metadata into the normalized record."""
        ...


class MarketDataSource(Protocol):
    """Live connectivity. Separated from :class:`VenueAdapter` so that replay,
    decoding, and tests never depend on anything that opens a socket."""

    async def fetch_markets(self, *, event_id: str | None = None) -> list[dict[str, Any]]:
        """Public market metadata."""
        ...

    async def fetch_orderbook(self, ticker: str) -> dict[str, Any]:
        """Current book snapshot for one market."""
        ...

    def stream(self, tickers: list[str]) -> AsyncIterator[tuple[str, dict[str, Any], dt.datetime]]:
        """Yield ``(channel, payload, received_ts)`` until cancelled.

        Implementations own reconnection and must surface it to the caller so
        that health counters and book invalidation stay accurate -- a
        reconnect that looks like an uninterrupted stream is how a gap becomes
        a silently wrong book.
        """
        ...
