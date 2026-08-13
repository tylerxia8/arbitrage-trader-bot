"""Resolve the live collection universe from the venue.

Answers "which markets should we be collecting right now" -- which is a live
question, not a configuration constant, because the recommended universe is
daily temperature partitions and they rotate. Yesterday's Atlanta event
already has zero active markets.

Only structurally complete partitions are returned. A set that is merely
mutually exclusive is not a basket (see :mod:`arbbot.venues.kalshi.discovery`
for what that mistake looks like priced), and collecting one would fill the
archive with legs that cannot form the thing M2 is looking for.

This selects what to *observe*. Nothing here approves a relationship or
implies one exists.
"""

from __future__ import annotations

from typing import Any, Final

from arbbot.venues.kalshi.discovery import check_integer_coverage, classify_event
from arbbot.venues.kalshi.rest import KalshiRestClient

__all__ = ["TEMPERATURE_PREFIXES", "UniverseResolver", "resolve_temperature_universe"]

#: Daily temperature series: highs, lows, and hourly directional temperature.
TEMPERATURE_PREFIXES: Final = ("KXHIGH", "KXLOW", "KXTEMP")

#: Cap on markets collected at once. At the client's 8 requests/second a cycle
#: must finish inside its poll interval, and an unbounded universe would let a
#: quiet day's worth of new listings silently stretch the cadence.
DEFAULT_MAX_MARKETS: Final = 120


class UniverseResolver:
    """Finds the live, structurally-complete partitions worth collecting."""

    def __init__(
        self,
        client: KalshiRestClient,
        *,
        prefixes: tuple[str, ...] = TEMPERATURE_PREFIXES,
        max_markets: int = DEFAULT_MAX_MARKETS,
        require_integer_coverage: bool = True,
    ) -> None:
        """
        :param client: the shared, rate-limited venue client. Required rather
            than optional: one resolver pass touches every temperature series,
            and doing that outside the limiter earns a 429 on the first run --
            which is exactly what happened the first time this was tried live.
            Sharing the client also means a mid-run refresh cannot throttle
            the collector, since both draw on the same limiter.
        """
        self._client = client
        self.prefixes = prefixes
        self.max_markets = max_markets
        self.require_integer_coverage = require_integer_coverage

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return (await self._client.fetch(path, params)).payload

    async def series_tickers(self) -> list[str]:
        body = await self._get("/series", {"category": "Climate and Weather"})
        return [
            s["ticker"]
            for s in body.get("series", [])
            if isinstance(s.get("ticker"), str) and s["ticker"].startswith(self.prefixes)
        ]

    async def resolve(self) -> list[str]:
        """Return the market tickers of every live, complete partition."""
        collected: list[str] = []

        for series in await self.series_tickers():
            body = await self._get(
                "/events",
                {
                    "series_ticker": series,
                    "limit": 4,
                    "with_nested_markets": "true",
                    "status": "open",
                },
            )
            for event in body.get("events") or []:
                markets = [m for m in event.get("markets") or [] if m.get("status") == "active"]
                if not classify_event(event, markets).may_propose:
                    continue
                if self.require_integer_coverage and not check_integer_coverage(markets).covered:
                    # A set with a hole is not a basket. Collecting it would
                    # archive legs that can never form the thing M2 looks for.
                    continue
                collected.extend(str(m["ticker"]) for m in markets)

        # Sorted so the selection is deterministic: an arbitrary cut of an
        # arbitrary order would silently change which markets are observed
        # from one refresh to the next.
        collected.sort()
        return collected[: self.max_markets]


async def resolve_temperature_universe(client: KalshiRestClient) -> list[str]:
    """Convenience :data:`~arbbot.collection.service.MarketSource`."""
    return await UniverseResolver(client).resolve()
