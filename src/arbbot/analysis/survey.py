"""Is there anywhere on this venue where the strategy has room?

Both routes through daily temperature partitions are measured and both are
negative: crossing the spread has no room at all, and quoting has room that
cannot be reached because the basket never completes. That is a real finding
and it is a *narrow* one. Temperature is the most obvious partition family on
the venue, which makes it the most competed one, and everything this system
does is universe-agnostic apart from which tickers it points at.

So before concluding anything about the venue, price every partition on it.

This is a **snapshot, not a distribution**. It prices each eligible event once,
at whatever moment the sweep reached it, and a single moment cannot say how
often a basket is cheap or for how long. What it can do is separate "nowhere on
this venue has room" from "temperature specifically has no room", and those
imply completely different next moves. Anything it flags is a candidate for
collection, never a candidate for trading.

Two things it refuses to do.

**It will not sum legs fetched far apart.** Every leg of an event is fetched in
one pass and the spread between the first and last fetch is recorded; an event
whose legs took too long is dropped rather than priced. This is the same
mistake that once reported a Boston basket at $0.38 by combining quotes four
hours apart, and a sweep across hundreds of events is exactly where it would
recur unnoticed.

**It will not price a structure it has not checked.** Only events that classify
as numeric partitions with both tails *and* pass the integer coverage check are
priced. A set with a hole is not a basket, and summing one produces a discount
that is really a missing outcome.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

from arbbot.money import PAYOUT_DOLLARS, ZERO
from arbbot.venues.kalshi.discovery import check_integer_coverage, classify_event
from arbbot.venues.kalshi.rest import KalshiRestClient

__all__ = ["EventPricing", "SurveyReport", "survey_venue"]

#: How far apart the first and last leg of one event may be fetched.
#:
#: Tight on purpose. A sweep priced over hundreds of events will always have
#: some event whose legs straddled a move, and a generous window turns that
#: into the most attractive row in the table.
MAX_LEG_SPREAD: Final = dt.timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class EventPricing:
    """One partition, priced once."""

    event_ticker: str
    series: str
    title: str
    legs: int
    taker_cost: Decimal
    """Sum of the YES asks: what crossing the spread on every leg costs."""

    maker_cost: Decimal
    """Sum of the YES bids: what resting on every leg would cost if filled."""

    min_depth: Decimal
    leg_spread: dt.timedelta
    """How long the whole event took to fetch. Evidence the sum is a moment."""

    @property
    def taker_edge(self) -> Decimal:
        return PAYOUT_DOLLARS - self.taker_cost

    @property
    def maker_edge(self) -> Decimal:
        return PAYOUT_DOLLARS - self.maker_cost

    @property
    def spread_width(self) -> Decimal:
        """Total spread across the basket -- what a maker would be paid to cross."""
        return self.taker_cost - self.maker_cost


@dataclass(slots=True)
class SurveyReport:
    """What one pass over the venue found."""

    priced: list[EventPricing] = field(default_factory=list)
    series_seen: int = 0
    events_seen: int = 0
    skipped_structure: int = 0
    skipped_coverage: int = 0
    skipped_stale: int = 0
    skipped_incomplete: int = 0
    series_errors: list[str] = field(default_factory=list)

    @property
    def taker_positive(self) -> list[EventPricing]:
        return [p for p in self.priced if p.taker_edge > ZERO]

    @property
    def maker_positive(self) -> list[EventPricing]:
        return [p for p in self.priced if p.maker_edge > ZERO]

    def render(self, limit: int = 20) -> str:
        lines = [
            f"series examined          : {self.series_seen}",
            f"events examined          : {self.events_seen}",
            f"priced                   : {len(self.priced)}",
            f"  not a partition        : {self.skipped_structure}",
            f"  buckets do not tile    : {self.skipped_coverage}",
            f"  legs fetched too far apart : {self.skipped_stale}",
            f"  book missing on a leg  : {self.skipped_incomplete}",
            f"  series that errored    : {len(self.series_errors)}",
        ]
        if not self.priced:
            lines.append("")
            lines.append("Nothing on this venue priced as a complete, verified partition.")
            return "\n".join(lines)

        lines.append("")
        lines.append(f"below a dollar crossing the spread : {len(self.taker_positive)}")
        lines.append(f"below a dollar resting at the bid  : {len(self.maker_positive)}")

        lines.append("")
        lines.append("cheapest partitions on the venue, by what crossing the spread costs:")
        lines.append(
            f"  {'event':<30} {'legs':>4} {'taker':>9} {'maker':>9} {'spread':>8} {'depth':>9}"
        )
        lines.append("  " + "-" * 78)
        for pricing in sorted(self.priced, key=lambda p: p.taker_cost)[:limit]:
            lines.append(
                f"  {pricing.event_ticker:<30} {pricing.legs:>4} "
                f"${pricing.taker_cost:>8} ${pricing.maker_cost:>8} "
                f"${pricing.spread_width:>7} {pricing.min_depth:>9}"
            )

        lines.append("")
        lines.append("One moment per event, so this says where to look and never how often.")
        lines.append("A cheap row here is a candidate for collection, not for trading: no")
        lines.append("relationship is approved, depth is the thinnest leg at one instant,")
        lines.append("and taker figures are before the fee that decided the last verdict.")
        return "\n".join(lines)


def _best_price(levels: list[list[str]] | None) -> tuple[Decimal, Decimal] | None:
    """Best resting bid on one side of the venue's book, and its size."""
    if not levels:
        return None
    best = max(levels, key=lambda level: Decimal(str(level[0])))
    return Decimal(str(best[0])), Decimal(str(best[1]))


async def survey_venue(
    client: KalshiRestClient,
    *,
    categories: list[str] | None = None,
    events_per_series: int = 2,
    max_events: int = 400,
    max_leg_spread: dt.timedelta = MAX_LEG_SPREAD,
    on_progress: Any = None,
) -> SurveyReport:
    """Price every structurally-eligible partition the venue currently lists."""
    report = SurveyReport()

    series_bodies: list[dict[str, Any]] = []
    for category in categories or [None]:  # type: ignore[list-item]
        params = {"category": category} if category else {}
        body = (await client.fetch("/series", params)).payload
        series_bodies.extend(body.get("series") or [])

    # dict.fromkeys rather than a set: the sweep order decides which series get
    # priced when the cap bites, and an arbitrary order would silently change
    # which part of the venue was examined between two runs.
    series_tickers = list(
        dict.fromkeys(
            s["ticker"] for s in series_bodies if isinstance(s.get("ticker"), str) and s["ticker"]
        )
    )
    report.series_seen = len(series_tickers)

    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for ticker in series_tickers:
        if len(candidates) >= max_events:
            break
        try:
            body = (
                await client.fetch(
                    "/events",
                    {
                        "series_ticker": ticker,
                        "limit": events_per_series,
                        "with_nested_markets": "true",
                        "status": "open",
                    },
                )
            ).payload
        except Exception as exc:
            # A sweep over hundreds of series will meet a few that error, and
            # losing the whole survey to one of them would be absurd. Counted
            # and named rather than swallowed, so a systematic failure cannot
            # hide as a small "priced" number.
            report.series_errors.append(f"{ticker}: {type(exc).__name__}")
            continue

        for event in body.get("events") or []:
            report.events_seen += 1
            markets = [m for m in event.get("markets") or [] if m.get("status") == "active"]
            if not classify_event(event, markets).may_propose:
                report.skipped_structure += 1
                continue
            if not check_integer_coverage(markets).covered:
                report.skipped_coverage += 1
                continue
            candidates.append((event, markets))

    if on_progress:
        on_progress(f"{len(candidates)} verified partitions to price; fetching books...")

    for index, (event, markets) in enumerate(candidates):
        tickers = [str(m["ticker"]) for m in markets]
        started = dt.datetime.now(dt.UTC)
        try:
            books = await asyncio.gather(*(client.fetch_orderbook(t) for t in tickers))
        except Exception:
            report.skipped_incomplete += 1
            continue
        spread = dt.datetime.now(dt.UTC) - started

        if spread > max_leg_spread:
            # The $0.38 Boston bug, at survey scale: a sum of quotes that never
            # coexisted is not a price, and across hundreds of events one of
            # them will always have straddled a move.
            report.skipped_stale += 1
            continue

        taker = ZERO
        maker = ZERO
        depth = None
        incomplete = False
        for fetched in books:
            book = fetched.payload.get("orderbook") or fetched.payload.get("orderbook_fp") or {}
            yes = _best_price(book.get("yes_dollars") or book.get("yes"))
            no = _best_price(book.get("no_dollars") or book.get("no"))
            if no is None:
                # No offer means the leg cannot be bought at any price, so the
                # basket has no cost rather than a cheap one.
                incomplete = True
                break
            taker += PAYOUT_DOLLARS - no[0]
            maker += yes[0] if yes else ZERO
            available = no[1]
            depth = available if depth is None else min(depth, available)

        if incomplete or depth is None:
            report.skipped_incomplete += 1
            continue

        report.priced.append(
            EventPricing(
                event_ticker=str(event.get("event_ticker", "")),
                series=str(event.get("series_ticker", "")),
                title=str(event.get("title", "")),
                legs=len(tickers),
                taker_cost=taker,
                maker_cost=maker,
                min_depth=depth,
                leg_spread=spread,
            )
        )
        if on_progress and (index + 1) % 25 == 0:
            on_progress(f"  priced {index + 1}/{len(candidates)}")

    return report
