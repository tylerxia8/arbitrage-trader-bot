"""Resolving the live collection universe.

The rotation problem this exists to solve: daily markets settle overnight, so
a collector started with a literal ticker list spends the rest of the week
polling dead contracts while reporting itself healthy.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from arbbot.venues.kalshi.rest import KalshiRestClient
from arbbot.venues.kalshi.universe import UniverseResolver


def partition_markets(prefix: str, *, complete: bool = True) -> list[dict[str, Any]]:
    """A six-bucket temperature partition, matching the live shape."""
    markets: list[dict[str, Any]] = [
        {"ticker": f"{prefix}-T91", "status": "active", "strike_type": "less", "cap_strike": "92"},
        {
            "ticker": f"{prefix}-T92",
            "status": "active",
            "strike_type": "between",
            "floor_strike": "92",
            "cap_strike": "93",
        },
        {
            "ticker": f"{prefix}-T94",
            "status": "active",
            "strike_type": "between",
            "floor_strike": "94",
            "cap_strike": "95",
        },
        {
            "ticker": f"{prefix}-T96",
            "status": "active",
            "strike_type": "greater" if complete else "between",
            "floor_strike": "95" if complete else "96",
            "cap_strike": None if complete else "97",
        },
    ]
    return markets


def named_markets(prefix: str) -> list[dict[str, Any]]:
    return [
        {"ticker": f"{prefix}-C{i}", "status": "active", "strike_type": "custom"} for i in range(5)
    ]


def build(events_by_series: dict[str, list[dict[str, Any]]], series: list[str]) -> UniverseResolver:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [{"ticker": t} for t in series]})
        ticker = request.url.params.get("series_ticker", "")
        return httpx.Response(200, json={"events": events_by_series.get(ticker, [])})

    return UniverseResolver(
        KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
    )


def event(ticker: str, markets: list[dict[str, Any]], exclusive: bool = True) -> dict[str, Any]:
    return {"event_ticker": ticker, "mutually_exclusive": exclusive, "markets": markets}


class TestSelection:
    async def test_collects_a_complete_partition(self) -> None:
        resolver = build(
            {"KXHIGHTATL": [event("KXHIGHTATL-D1", partition_markets("KXHIGHTATL-D1"))]},
            ["KXHIGHTATL"],
        )
        assert len(await resolver.resolve()) == 4

    async def test_skips_named_candidate_sets(self) -> None:
        """Collecting these would fill the archive with legs that can never
        form a basket -- and priced, they are the $0.746 trap."""
        resolver = build(
            {"KXHIGHTATL": [event("KXPRESMATCHUP", named_markets("KXPRESMATCHUP"))]},
            ["KXHIGHTATL"],
        )
        assert await resolver.resolve() == []

    async def test_skips_partitions_with_a_hole(self) -> None:
        resolver = build(
            {
                "KXHIGHTATL": [
                    event("KXHIGHTATL-D1", partition_markets("KXHIGHTATL-D1", complete=False))
                ]
            },
            ["KXHIGHTATL"],
        )
        assert await resolver.resolve() == []

    async def test_ignores_settled_legs(self) -> None:
        """A settled leg cannot be bought, so a set that only looks complete
        because of one is not complete."""
        markets = partition_markets("KXHIGHTATL-D1")
        markets[0]["status"] = "settled"
        resolver = build({"KXHIGHTATL": [event("KXHIGHTATL-D1", markets)]}, ["KXHIGHTATL"])
        assert await resolver.resolve() == []

    async def test_only_temperature_series_are_considered(self) -> None:
        resolver = build(
            {"KXHURPATHFLA": [event("KXHURPATHFLA-1", partition_markets("KXHURPATHFLA-1"))]},
            ["KXHURPATHFLA"],
        )
        assert await resolver.resolve() == []


class TestRotation:
    async def test_resolves_whichever_day_is_live(self) -> None:
        """The point of resolving at all: today's event, not the one hard-coded
        on the day the run started."""
        today = build(
            {"KXHIGHTATL": [event("KXHIGHTATL-D2", partition_markets("KXHIGHTATL-D2"))]},
            ["KXHIGHTATL"],
        )
        tickers = await today.resolve()
        assert all("D2" in t for t in tickers)

    async def test_multiple_series_are_combined(self) -> None:
        resolver = build(
            {
                "KXHIGHTATL": [event("A", partition_markets("A"))],
                "KXLOWTSFO": [event("B", partition_markets("B"))],
            },
            ["KXHIGHTATL", "KXLOWTSFO"],
        )
        assert len(await resolver.resolve()) == 8


class TestBounds:
    async def test_the_universe_is_capped(self) -> None:
        """A cycle must finish inside its poll interval; an unbounded universe
        would let a busy day silently stretch the cadence."""
        events = {f"KXHIGH{i}": [event(f"E{i}", partition_markets(f"E{i}"))] for i in range(50)}
        resolver = build(events, list(events))
        resolver.max_markets = 10
        assert len(await resolver.resolve()) == 10

    async def test_selection_is_deterministic(self) -> None:
        """An arbitrary cut of an arbitrary order would change which markets
        are observed from one refresh to the next."""
        events = {f"KXHIGH{i}": [event(f"E{i}", partition_markets(f"E{i}"))] for i in range(20)}
        resolver = build(events, list(events))
        resolver.max_markets = 5
        assert await resolver.resolve() == await resolver.resolve()


class TestFailure:
    async def test_a_venue_error_propagates_to_the_caller(self) -> None:
        """The service decides what to do about it -- and keeps the markets it
        already has, so a hiccup costs freshness, never continuity."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        resolver = UniverseResolver(
            KalshiRestClient(
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                requests_per_second=10_000,
                max_attempts=1,
            )
        )
        with pytest.raises(httpx.HTTPStatusError):
            await resolver.resolve()
