"""Kalshi venue integration.

Verified against live documentation and the production API on 2026-08-12.

**Only public REST is implemented.** ``GET /markets`` and
``GET /markets/{ticker}/orderbook`` both return data with no credential --
confirmed by calling them, not by reading about them, because the API
reference and the market-data quick-start disagreed on exactly that point.

The WebSocket feed is *not* implemented, and the reason is a decision the
owner has to make rather than a gap in the work. Kalshi requires API-key
headers on the WebSocket handshake even for channels carrying public data, so
streaming collection needs a credential. Milestone 1 is specified to run
before any credential is authorised, and :mod:`arbbot.config` actively rejects
one in the research environment. Polling REST is therefore the credential-free
path to continuous collection; the trade-off is coarser time resolution than
``orderbook_delta`` would give. See ``docs/venue-findings.md``.
"""

from __future__ import annotations

from typing import Any

from arbbot.marketdata.types import BookEvent, MarketSnapshotRecord, SnapshotEvent
from arbbot.venues.kalshi.parse import (
    SCHEMA_VERSION,
    VENUE,
    KalshiParseError,
    decode_book_event,
    decode_market,
    decode_rest_orderbook,
)
from arbbot.venues.kalshi.rest import (
    DEMO_REST_BASE,
    PRODUCTION_REST_BASE,
    KalshiRestClient,
)

__all__ = [
    "DEMO_REST_BASE",
    "PRODUCTION_REST_BASE",
    "SCHEMA_VERSION",
    "VENUE",
    "KalshiAdapter",
    "KalshiParseError",
    "KalshiRestClient",
]


class KalshiAdapter:
    """Offline decoding half of the Kalshi integration.

    Deliberately free of I/O so that replay, decoding, and tests never depend
    on anything that opens a socket. Live connectivity lives in
    :class:`~arbbot.venues.kalshi.rest.KalshiRestClient`.
    """

    @property
    def venue(self) -> str:
        return VENUE

    @property
    def schema_version(self) -> str:
        return SCHEMA_VERSION

    def subscription_key(self, channel: str, ticker: str) -> str:
        """Identity of one market's stream on one channel.

        Kalshi issues a single ``sid`` per channel subscription and reuses it
        across every market in that subscription, so the ``sid`` alone cannot
        scope a sequence number to a market. The ticker is part of the key for
        that reason: without it, one market's message 5 would collide with
        another's in the archive.
        """
        return f"{channel}:{ticker}"

    def decode_book_event(self, payload: dict[str, Any]) -> BookEvent | None:
        return decode_book_event(payload)

    def decode_market(self, payload: dict[str, Any]) -> MarketSnapshotRecord:
        return decode_market(payload)

    def decode_rest_orderbook(
        self, ticker: str, payload: dict[str, Any], sequence: int
    ) -> SnapshotEvent:
        return decode_rest_orderbook(ticker, payload, sequence)
