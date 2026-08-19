"""Pricing every partition on the venue once.

A sweep across hundreds of events is exactly where a quiet pricing bug becomes
invisible: one bad row looks like a discovery rather than a defect, and it will
be the most attractive row in the table. These tests pin the refusals.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import httpx

from arbbot.analysis.survey import survey_venue
from arbbot.venues.kalshi.rest import KalshiRestClient

D = Decimal


def market(
    suffix: str, *, strike_type: str, floor: str | None = None, cap: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": f"KXTEST-26AUG14-{suffix}",
        "status": "active",
        "strike_type": strike_type,
        "rules_primary": "settles from the test source",
    }
    if floor is not None:
        payload["floor_strike"] = floor
    if cap is not None:
        payload["cap_strike"] = cap
    return payload


PARTITION = [
    market("T98", strike_type="less", cap="99"),
    market("B99", strike_type="between", floor="99", cap="100"),
    market("T100", strike_type="greater", floor="100"),
]

CATEGORICAL = [
    {"ticker": f"KXTEST-26AUG14-{n}", "status": "active", "strike_type": "custom"}
    for n in ("ALICE", "BOB", "CAROL")
]

#: Both tails and enough legs to classify as a partition, but 99 and 100 fall
#: through the gap. This is the shape that has to reach the coverage check --
#: a two-leg version is rejected for being too small and never tests it.
HOLED = [
    market("T98", strike_type="less", cap="99"),
    market("B101", strike_type="between", floor="101", cap="102"),
    market("T102", strike_type="greater", floor="102"),
]


def venue(
    markets: list[dict[str, Any]], *, no_bid: str = "0.70", yes_bid: str = "0.25"
) -> KalshiRestClient:
    """A venue with one series, one event, and a uniform book on every leg."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/series"):
            return httpx.Response(200, json={"series": [{"ticker": "KXTEST"}]})
        if path.endswith("/events"):
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_ticker": "KXTEST-26AUG14",
                            "series_ticker": "KXTEST",
                            "title": "a test partition",
                            "mutually_exclusive": True,
                            "markets": markets,
                        }
                    ]
                },
            )
        if "orderbook" in path:
            return httpx.Response(
                200,
                json={
                    "orderbook": {
                        "yes": [[yes_bid, "500"]],
                        "no": [[no_bid, "500"]],
                    }
                },
            )
        return httpx.Response(404)

    return KalshiRestClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        requests_per_second=10_000,
        max_attempts=1,
    )


class TestPricing:
    async def test_a_verified_partition_is_priced_both_ways(self) -> None:
        """Taker cost is the sum of the asks, maker cost the sum of the bids.
        The gap between them is the spread, which is what decided the last
        verdict."""
        async with venue(PARTITION, no_bid="0.70", yes_bid="0.25") as client:
            report = await survey_venue(client)

        assert len(report.priced) == 1
        pricing = report.priced[0]
        assert pricing.taker_cost == D("0.90")  # three legs at 1.00 - 0.70
        assert pricing.maker_cost == D("0.75")  # three legs at 0.25
        assert pricing.spread_width == D("0.15")

    async def test_depth_is_the_thinnest_leg(self) -> None:
        async with venue(PARTITION) as client:
            report = await survey_venue(client)
        assert report.priced[0].min_depth == D("500")


class TestStructuralRefusals:
    async def test_a_categorical_set_is_now_surveyed(self) -> None:
        """The sweep used to skip these, which is how a five-outcome Fed basket
        at ninety cents went unseen across eleven thousand events."""
        async with venue(CATEGORICAL) as client:
            report = await survey_venue(client)

        assert report.skipped_structure == 0
        assert len(report.structures) == 1

    async def test_buckets_with_a_hole_are_not_priced(self) -> None:
        """A set with a gap produces a discount that is really a missing
        outcome -- the most dangerous row a sweep can print."""
        async with venue(HOLED) as client:
            report = await survey_venue(client)

        assert report.priced == []
        assert report.skipped_coverage == 1


class TestPricingRefusals:
    async def test_a_leg_with_no_offer_is_not_priced(self) -> None:
        """No offer means the leg cannot be bought at any price. Treating the
        missing side as free would make an unbuyable basket the cheapest one."""

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/series"):
                return httpx.Response(200, json={"series": [{"ticker": "KXTEST"}]})
            if path.endswith("/events"):
                return httpx.Response(
                    200,
                    json={
                        "events": [
                            {
                                "event_ticker": "KXTEST-26AUG14",
                                "series_ticker": "KXTEST",
                                "mutually_exclusive": True,
                                "markets": PARTITION,
                            }
                        ]
                    },
                )
            if "T100" in path:
                return httpx.Response(200, json={"orderbook": {"yes": [["0.25", "5"]], "no": []}})
            return httpx.Response(
                200, json={"orderbook": {"yes": [["0.25", "5"]], "no": [["0.70", "5"]]}}
            )

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            report = await survey_venue(client)

        assert report.priced == []
        assert report.skipped_incomplete == 1

    async def test_legs_fetched_too_far_apart_are_dropped(self) -> None:
        """The Boston bug at survey scale. Across hundreds of events one will
        always straddle a move, and a sum of quotes that never coexisted is not
        a price."""
        async with venue(PARTITION) as client:
            report = await survey_venue(client, max_leg_spread=dt.timedelta(0))

        assert report.priced == []
        assert report.skipped_stale == 1


class TestEnumeration:
    """Events are paged directly rather than walked series by series.

    The venue lists over thirteen thousand series. One /events call each is
    thirteen thousand requests before a single book is fetched, and that design
    was a meaningful part of what got this address blocked.
    """

    async def test_the_whole_sweep_costs_one_request_per_page(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/events"):
                return httpx.Response(
                    200,
                    json={
                        "events": [
                            {
                                "event_ticker": "KXTEST-26AUG14",
                                "series_ticker": "KXTEST",
                                "mutually_exclusive": True,
                                "markets": PARTITION,
                            }
                        ]
                    },
                )
            return httpx.Response(
                200, json={"orderbook": {"yes": [["0.25", "5"]], "no": [["0.70", "5"]]}}
            )

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            report = await survey_venue(client, price_books=False)

        assert len(report.structures) == 1
        assert sum(1 for c in calls if c.endswith("/events")) == 1
        assert not any(c.endswith("/series") for c in calls), "no per-series walk"

    async def test_a_cursor_that_stops_advancing_ends_the_sweep(self) -> None:
        """A server echoing the same cursor forever would otherwise spin until
        the rate limiter turned it into a very slow infinite loop."""
        pages = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal pages
            pages += 1
            return httpx.Response(
                200,
                json={
                    "cursor": "always-the-same",
                    "events": [
                        {
                            "event_ticker": "KXTEST-26AUG14",
                            "series_ticker": "KXTEST",
                            "mutually_exclusive": True,
                            "markets": PARTITION,
                        }
                    ],
                },
            )

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            await survey_venue(client, price_books=False, max_pages=50)

        assert pages == 2, "one page, then the repeated cursor stops it"

    async def test_pages_are_capped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "cursor": f"c{id(request)}",
                    "events": [
                        {
                            "event_ticker": f"KXTEST-{id(request)}",
                            "series_ticker": "KXTEST",
                            "mutually_exclusive": True,
                            "markets": PARTITION,
                        }
                    ],
                },
            )

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            report = await survey_venue(client, price_books=False, max_pages=3, max_events=10_000)

        assert report.events_seen == 3


class TestReport:
    async def test_the_report_states_it_is_one_moment(self) -> None:
        async with venue(PARTITION) as client:
            rendered = (await survey_venue(client)).render()

        assert "One moment per event" in rendered
        assert "candidate for collection, not for trading" in rendered

    async def test_a_venue_with_nothing_priceable_says_so(self) -> None:
        """A categorical set no longer stands in for "unpriceable" -- those are
        surveyed now. A two-outcome event genuinely is not a basket."""
        too_few = [market("A", strike_type="less", cap="99")]
        async with venue(too_few) as client:
            rendered = (await survey_venue(client)).render()
        assert "Nothing on this venue priced" in rendered

    async def test_a_failed_page_is_counted_not_swallowed(self) -> None:
        """A systematic failure must not hide behind a small "priced" number."""
        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            report = await survey_venue(client)

        assert len(report.series_errors) == 1
        assert "page 0" in report.series_errors[0]
        assert report.priced == []
