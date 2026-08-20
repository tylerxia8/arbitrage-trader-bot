"""Kalshi public REST endpoints.

Uses only endpoints verified to work **without credentials**: ``GET /markets``
and ``GET /markets/{ticker}/orderbook``. That matters for Milestone 1, which
must collect evidence before any key exists -- and the configuration layer
actively refuses a credential in the research environment, so this client is
built to never need one.

Everything about *how* to talk to a rate-limited API -- the limiter, the retry
ladder, the circuit breaker -- moved to :mod:`arbbot.venues.http` when a second
venue arrived. None of it was ever a Kalshi fact, and a second venue that
reimplemented it would reimplement the three outages that shaped it.
"""

from __future__ import annotations

from typing import Any, Final

from arbbot.venues.http import DEFAULT_RATE_LIMIT, FetchedPayload, RateLimiter, VenueHttpClient
from arbbot.venues.http import VenueUnreachable as VenueUnreachable
from arbbot.venues.kalshi.parse import SCHEMA_VERSION, VENUE

__all__ = [
    "DEFAULT_RATE_LIMIT",
    "DEMO_REST_BASE",
    "PRODUCTION_REST_BASE",
    "FetchedPayload",
    "KalshiRestClient",
    "RateLimiter",
    "VenueUnreachable",
]

#: Verified against the live API on 2026-08-12.
PRODUCTION_REST_BASE: Final = "https://external-api.kalshi.com/trade-api/v2"
DEMO_REST_BASE: Final = "https://external-api.demo.kalshi.co/trade-api/v2"


class KalshiRestClient(VenueHttpClient):
    """Public market-data client. Never sends credentials."""

    def __init__(self, base_url: str = PRODUCTION_REST_BASE, **kwargs: Any) -> None:
        super().__init__(base_url, **kwargs)
        self.venue = VENUE
        self.schema_version = SCHEMA_VERSION

    async def fetch_markets(
        self,
        *,
        limit: int = 200,
        cursor: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
    ) -> FetchedPayload:
        """One page of ``GET /markets``. Public; no credential required."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return await self.fetch("/markets", params)

    async def iter_markets(
        self, *, series_ticker: str | None = None, page_limit: int = 200
    ) -> list[dict[str, Any]]:
        """Every market, following the cursor to exhaustion.

        Stops when the cursor stops advancing as well as when it empties: a
        server that echoes the same cursor forever would otherwise spin this
        loop until the rate limiter throttled it into a very slow infinite
        loop, which is the least debuggable kind.
        """
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()

        while True:
            page = await self.fetch_markets(
                limit=page_limit, cursor=cursor, series_ticker=series_ticker
            )
            markets = page.payload.get("markets") or []
            collected.extend(markets)

            cursor = page.payload.get("cursor") or None
            if not cursor or cursor in seen_cursors or not markets:
                return collected
            seen_cursors.add(cursor)

    async def fetch_orderbook(self, ticker: str, *, depth: int = 0) -> FetchedPayload:
        """``GET /markets/{ticker}/orderbook``. Public; no credential required.

        ``depth=0`` requests every level. Partial depth is a false economy for
        arbitrage: a basket's executable cost depends on levels beyond the top
        of book, and a truncated book quietly understates what a fill costs.
        """
        return await self.fetch(f"/markets/{ticker}/orderbook", {"depth": depth})

    # -- transport -------------------------------------------------------
