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
class StructureFinding:
    """One event that is shaped like a basket, whatever it currently costs.

    Recorded separately from pricing because the two questions have different
    evidence requirements. Whether six buckets tile the integers is a fact
    about the market's definition, and the demo host publishes the same
    definitions as production -- so structure can be answered while production
    is unreachable. What the basket costs cannot: demo liquidity is not real,
    and pricing against it would produce a number that looks like a finding.
    """

    event_ticker: str
    series: str
    title: str
    legs: int


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
    structures: list[StructureFinding] = field(default_factory=list)
    """Every verified partition found, priced or not."""

    priced_books: bool = True
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

        if self.structures:
            by_series: dict[str, list[StructureFinding]] = {}
            for finding in self.structures:
                by_series.setdefault(finding.series or "(unknown)", []).append(finding)

            lines.append("")
            lines.append(f"verified partitions found : {len(self.structures)}")
            lines.append(f"across distinct series    : {len(by_series)}")
            lines.append("")
            lines.append(f"  {'series':<34} {'events':>7} {'legs':>6}  example")
            lines.append("  " + "-" * 84)
            for series, found in sorted(by_series.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                legs = sorted({f.legs for f in found})
                shape = str(legs[0]) if len(legs) == 1 else f"{legs[0]}-{legs[-1]}"
                lines.append(f"  {series:<34} {len(found):>7} {shape:>6}  {found[0].event_ticker}")

        if not self.priced_books:
            lines.append("")
            lines.append("STRUCTURE ONLY: no books were fetched and nothing here is priced.")
            lines.append("Whether these buckets tile the integers is a fact about the market's")
            lines.append("definition and is answered above. Whether any of them trades below a")
            lines.append("dollar is not, and cannot be answered from a venue whose liquidity is")
            lines.append("not real. That half waits for production.")
            return "\n".join(lines)

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
    max_events: int = 400,
    page_size: int = 200,
    max_pages: int = 40,
    series: tuple[str, ...] = (),
    max_leg_spread: dt.timedelta = MAX_LEG_SPREAD,
    price_books: bool = True,
    on_progress: Any = None,
) -> SurveyReport:
    """Find, and optionally price, every verified partition the venue lists.

    ``price_books=False`` answers only the structural half: which market
    families are shaped like baskets. That half is answerable from any host
    publishing the same market definitions -- including the demo host while
    production is unreachable -- because whether buckets tile the integers is a
    property of the definition. Pricing is not, and is skipped rather than
    computed from liquidity that is not real.
    """
    report = SurveyReport(priced_books=price_books)

    # Events are paged directly rather than walked series by series. The
    # difference is not a nicety: this venue lists over thirteen thousand
    # series, so one /events call each is thirteen thousand requests before a
    # single book is fetched -- three quarters of an hour of pure enumeration,
    # and a meaningful part of what got this address blocked. Paging /events
    # returns thousands of events per request. Same answer, three orders of
    # magnitude fewer calls.
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    seen_series: set[str] = set()
    cursor: str | None = None

    # Named series are fetched one request each rather than by paging the whole
    # venue. For a handful of families that is the cheaper direction by two
    # orders of magnitude, and the first thing run against a newly restored
    # address should be small: the last full sweep from here contributed to
    # losing the address in the first place.
    if series:
        for ticker in series:
            try:
                body = (
                    await client.fetch(
                        "/events",
                        {
                            "series_ticker": ticker,
                            "status": "open",
                            "with_nested_markets": "true",
                            "limit": page_size,
                        },
                    )
                ).payload
            except Exception as exc:
                report.series_errors.append(f"{ticker}: {type(exc).__name__}")
                continue
            _absorb(report, body.get("events") or [], candidates, seen_series)
            if on_progress:
                on_progress(f"  {ticker}: {len(candidates)} verified partitions so far")
        report.series_seen = len(seen_series)
        return await _price(report, client, candidates, max_leg_spread, price_books, on_progress)

    for page in range(max_pages):
        params: dict[str, Any] = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": page_size,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            body = (await client.fetch("/events", params)).payload
        except Exception as exc:
            # Counted and named rather than swallowed, so a systematic failure
            # cannot hide behind a small "priced" number.
            report.series_errors.append(f"page {page}: {type(exc).__name__}")
            break

        events = body.get("events") or []
        if not events:
            break

        _absorb(report, events, candidates, seen_series)

        if on_progress:
            on_progress(
                f"  page {page + 1}: {report.events_seen:,} events, "
                f"{len(candidates):,} verified partitions"
            )

        # Stops when the cursor empties *and* when it stops advancing. A server
        # echoing the same cursor forever would otherwise spin here until the
        # rate limiter turned it into a very slow infinite loop.
        next_cursor = body.get("cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = str(next_cursor)
        if len(candidates) >= max_events:
            break

    report.series_seen = len(seen_series)
    return await _price(report, client, candidates, max_leg_spread, price_books, on_progress)


def _absorb(
    report: SurveyReport,
    events: list[dict[str, Any]],
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    seen_series: set[str],
) -> None:
    """Classify a page of events, keeping the ones shaped like baskets."""
    for event in events:
        report.events_seen += 1
        series_ticker = str(event.get("series_ticker") or "")
        seen_series.add(series_ticker)
        markets = [m for m in event.get("markets") or [] if m.get("status") == "active"]
        structure = classify_event(event, markets)
        if not structure.may_propose:
            report.skipped_structure += 1
            continue
        # Coverage is only checkable where there are strikes to tile. A
        # categorical set has none, and running the check on it would reject
        # every one of them -- which is precisely how a five-outcome Fed basket
        # at ninety cents stayed invisible across eleven thousand events.
        if structure.verdict.coverage_is_checkable and not check_integer_coverage(markets).covered:
            report.skipped_coverage += 1
            continue
        candidates.append((event, markets))
        report.structures.append(
            StructureFinding(
                event_ticker=str(event.get("event_ticker", "")),
                series=series_ticker,
                title=str(event.get("title", "")),
                legs=len(markets),
            )
        )


async def _price(
    report: SurveyReport,
    client: KalshiRestClient,
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    max_leg_spread: dt.timedelta,
    price_books: bool,
    on_progress: Any,
) -> SurveyReport:
    """Fetch each candidate's books and price it, unless pricing is off."""
    if not price_books:
        if on_progress:
            on_progress(f"{len(candidates)} verified partitions found; not pricing")
        return report

    if on_progress:
        on_progress(
            f"{report.series_seen} series, {report.events_seen} events examined; "
            f"{len(candidates)} verified partitions to price"
        )

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
        if on_progress and (index + 1) % 20 == 0:
            on_progress(f"  priced {index + 1}/{len(candidates)}")

    return report
