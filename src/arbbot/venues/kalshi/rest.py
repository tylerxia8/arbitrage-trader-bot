"""Kalshi public REST client.

Uses only endpoints verified to work **without credentials**: ``GET /markets``
and ``GET /markets/{ticker}/orderbook``. That matters for Milestone 1, which
must collect evidence before any key exists -- and the configuration layer
actively refuses a credential in the research environment, so this client is
built to never need one.

Rate limiting is a token bucket at the venue: most calls cost 10 tokens, and
the Basic read tier refills 200 tokens/second, so ~20 requests/second is the
sustainable ceiling. The limiter here is deliberately set below that. A 429
during a seven-day collection run leaves a hole in the archive, and a hole is
not recoverable after the fact -- the venue does not re-send what we did not
ask for in time.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

from arbbot.venues.kalshi.parse import SCHEMA_VERSION, VENUE

__all__ = [
    "DEMO_REST_BASE",
    "PRODUCTION_REST_BASE",
    "KalshiRestClient",
    "RateLimiter",
]

#: Verified against the live API on 2026-08-12.
PRODUCTION_REST_BASE: Final = "https://external-api.kalshi.com/trade-api/v2"
DEMO_REST_BASE: Final = "https://external-api.demo.kalshi.co/trade-api/v2"

#: Requests per second. Well under the Basic tier's ~20/s so that ordinary
#: collection never approaches the limit; raising it buys nothing, because the
#: binding constraint on this system is evidence quality, not throughput.
DEFAULT_RATE_LIMIT: Final = 8

#: A 429 means we already lost. Back off hard rather than politely.
_BACKOFF_BASE_SECONDS: Final = 1.0
_BACKOFF_MAX_SECONDS: Final = 60.0
_MAX_ATTEMPTS: Final = 6


class RateLimiter:
    """Simple asynchronous rate limiter.

    Spaces requests by a minimum interval rather than modelling the venue's
    token bucket exactly. Under-approximating a rate limit is safe; a precise
    model that drifts is not.
    """

    def __init__(self, requests_per_second: int = DEFAULT_RATE_LIMIT) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_allowed = now + self._interval


@dataclass(frozen=True, slots=True)
class FetchedPayload:
    """A payload plus the moment it arrived.

    Receive time comes from the client rather than the venue: staleness is
    measured against when *we* saw the data, since a venue clock we do not
    control is not evidence about our own latency.
    """

    payload: dict[str, Any]
    received_ts: dt.datetime
    endpoint: str


class KalshiRestClient:
    """Public market-data client. Never sends credentials."""

    def __init__(
        self,
        base_url: str = PRODUCTION_REST_BASE,
        *,
        requests_per_second: int = DEFAULT_RATE_LIMIT,
        timeout_seconds: float = 15.0,
        max_attempts: int = _MAX_ATTEMPTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """
        :param max_attempts: how many times to try a request before giving up.
            The default suits a one-off fetch. A caller on a short polling
            cadence should lower it: the full backoff ladder spends about
            thirty seconds before surrendering, which is far longer than a
            five-second poll interval and would stall every other market in
            the cycle behind one broken one.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.venue = VENUE
        self.schema_version = SCHEMA_VERSION
        self.max_attempts = max_attempts
        self._limiter = RateLimiter(requests_per_second)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json", "User-Agent": "arbbot/0.1 (research)"},
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- endpoints -------------------------------------------------------
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
        return await self._get("/markets", params)

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
        return await self._get(f"/markets/{ticker}/orderbook", {"depth": depth})

    # -- transport -------------------------------------------------------
    async def _get(self, path: str, params: dict[str, Any]) -> FetchedPayload:
        url = f"{self.base_url}{path}"
        delay = _BACKOFF_BASE_SECONDS

        for attempt in range(1, self.max_attempts + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.get(url, params=params)
            except httpx.TransportError:
                if attempt == self.max_attempts:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_MAX_SECONDS)
                continue

            # The venue publishes no Retry-After; the bucket refills
            # continuously, so exponential backoff is the documented remedy.
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS or (
                response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
            ):
                if attempt == self.max_attempts:
                    response.raise_for_status()
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_MAX_SECONDS)
                continue

            response.raise_for_status()
            received = dt.datetime.now(dt.UTC)
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError(f"{path} returned {type(body).__name__}, expected an object")
            return FetchedPayload(payload=body, received_ts=received, endpoint=path)

        raise RuntimeError("unreachable: retry loop exited without returning")
