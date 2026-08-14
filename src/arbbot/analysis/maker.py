"""Would resting orders have done better than crossing the spread?

The taker verdict is in and it is negative: across the window where quote
freshness is actually measured, no basket cleared its fees at any staleness
threshold, and the binding constraint is net edge rather than latency. A
six-leg basket pays six crossings of the spread and the discount does not
cover them.

That result points at one structural lever. The venue's published schedule
charges **no maker fee at all** on its standard series, so the entire cost
model this system has been fighting is the price of immediacy. An order that
rests instead of crossing pays nothing. This module asks whether that changes
the arithmetic.

It is a different strategy, not a cheaper version of the same one, and the
three things that make it different are exactly what this measures.

**Cost.** Assembling passively costs the sum of the *bids*, not the sum of the
asks. That is the spread, and on this venue it is most of the difference
between a basket that loses money and one that does not.

**Fills are not free, they are uncertain.** A resting order is not a position.
The archive cannot see trades, only books, so a fill is inferred: a resting YES
bid at ``P`` is treated as filled once the derived YES ask falls to ``P`` or
below, because a seller offering at or under a standing bid is a crossed book
and cannot persist. That inference is *optimistic about queue position* unless
the order improves the best bid, so improving is the only mode measured for
fills -- joining a queue behind existing size would report fills this system
could not have received.

**Adverse selection is the cost that replaces fees.** A resting bid fills
precisely when someone wants to sell, which is precisely when the price is
about to fall. Measuring fill rate without measuring what happened *after* the
fill is how a maker backtest talks itself into a business. So every simulated
fill is followed forward and scored on where the market went.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.analysis.baskets import ConfirmationIndex, event_of
from arbbot.db.models import BookSnapshot
from arbbot.money import PAYOUT_DOLLARS, PRICE_QUANTUM, ZERO

__all__ = [
    "DEFAULT_FILL_HORIZON",
    "LegQuote",
    "MakerReport",
    "MakerSnapshot",
    "scan_maker_capacity",
]

#: How long a resting order is given to fill before it is counted as unfilled.
#:
#: A basket assembled over more than a couple of minutes is not an arbitrage
#: on a market that reprices all day; it is six separate directional bets
#: acquired at different times. Long horizons make fill rates look wonderful
#: and mean nothing.
DEFAULT_FILL_HORIZON: Final = dt.timedelta(minutes=2)

#: How far ahead to look when scoring what happened after a fill.
ADVERSE_SELECTION_HORIZON: Final = dt.timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class LegQuote:
    """One leg's two-sided market at a moment."""

    ticker: str
    bid: Decimal
    """Best resting YES bid: what a passive buyer would join."""

    ask: Decimal
    """Cheapest YES offer, derived from the opposite side's best NO bid."""

    bid_size: Decimal
    at: dt.datetime

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def improved_bid(self) -> Decimal:
        """One tick inside the bid -- alone at the front of the queue.

        The only price this module will claim a fill at. Joining the existing
        best bid puts an order behind size the archive cannot see traded, so a
        fill inferred there would be a fill this system might never receive.
        """
        return self.bid + PRICE_QUANTUM


@dataclass(frozen=True, slots=True)
class MakerSnapshot:
    """One moment at which a whole basket could have been quoted."""

    event: str
    at: dt.datetime
    legs: tuple[LegQuote, ...]

    @property
    def passive_cost(self) -> Decimal:
        """What the basket costs if every leg fills at the bid."""
        return sum((leg.bid for leg in self.legs), ZERO)

    @property
    def improved_cost(self) -> Decimal:
        """What it costs after paying a tick per leg for queue priority."""
        return sum((leg.improved_bid for leg in self.legs), ZERO)

    @property
    def aggressive_cost(self) -> Decimal:
        """What crossing the spread would cost. The taker path, for comparison."""
        return sum((leg.ask for leg in self.legs), ZERO)

    @property
    def passive_edge(self) -> Decimal:
        return PAYOUT_DOLLARS - self.passive_cost

    @property
    def improved_edge(self) -> Decimal:
        return PAYOUT_DOLLARS - self.improved_cost


@dataclass(slots=True)
class MakerReport:
    """What the archive says about quoting rather than crossing."""

    moments: int = 0
    """Basket moments where every leg had a two-sided market and fresh legs."""

    passive_below_payout: int = 0
    improved_below_payout: int = 0
    aggressive_below_payout: int = 0

    passive_costs: list[Decimal] = field(default_factory=list)
    improved_costs: list[Decimal] = field(default_factory=list)
    aggressive_costs: list[Decimal] = field(default_factory=list)

    legs_quoted: int = 0
    legs_filled: int = 0
    baskets_attempted: int = 0
    baskets_completed: int = 0

    adverse_scored: int = 0
    adverse_against: int = 0
    """Fills after which the market moved further in the seller's favour --
    the order was picked off."""

    events_seen: int = 0
    horizon: dt.timedelta = DEFAULT_FILL_HORIZON

    @property
    def leg_fill_rate(self) -> Decimal:
        if not self.legs_quoted:
            return ZERO
        return Decimal(self.legs_filled) / Decimal(self.legs_quoted)

    @property
    def basket_completion_rate(self) -> Decimal:
        if not self.baskets_attempted:
            return ZERO
        return Decimal(self.baskets_completed) / Decimal(self.baskets_attempted)

    @property
    def adverse_rate(self) -> Decimal:
        if not self.adverse_scored:
            return ZERO
        return Decimal(self.adverse_against) / Decimal(self.adverse_scored)

    def render(self) -> str:
        lines = [
            f"events observed          : {self.events_seen}",
            f"two-sided basket moments : {self.moments:,}",
        ]
        if not self.moments:
            lines.append("")
            lines.append("No moment had a resting bid on every leg. Nothing to quote against.")
            return "\n".join(lines)

        lines.append("")
        lines.append("what a basket costs, three ways:")
        lines.append(f"  {'route':<28} {'median':>9} {'cheapest':>10} {'under $1':>10}")
        for label, costs, under in (
            ("cross the spread (taker)", self.aggressive_costs, self.aggressive_below_payout),
            ("rest at the bid", self.passive_costs, self.passive_below_payout),
            ("rest one tick inside", self.improved_costs, self.improved_below_payout),
        ):
            if not costs:
                continue
            ordered = sorted(costs)
            share = under * 100 // max(self.moments, 1)
            lines.append(
                f"  {label:<28} ${ordered[len(ordered) // 2]:>8} ${ordered[0]:>9} "
                f"{under:>6,} ({share}%)"
            )

        lines.append("")
        lines.append(f"resting one tick inside, {self.horizon.total_seconds():g}s to fill:")
        lines.append(
            f"  legs quoted / filled    : {self.legs_quoted:,} / {self.legs_filled:,} "
            f"({self.leg_fill_rate * 100:.1f}%)"
        )
        lines.append(
            f"  baskets attempted/whole : {self.baskets_attempted:,} / "
            f"{self.baskets_completed:,} ({self.basket_completion_rate * 100:.1f}%)"
        )
        if self.adverse_scored:
            lines.append(
                f"  fills picked off        : {self.adverse_against:,} of "
                f"{self.adverse_scored:,} ({self.adverse_rate * 100:.1f}%)"
            )

        lines.append("")
        lines.append("A partial basket is a directional position nobody chose, and a maker")
        lines.append("controls neither which legs fill nor when. Fills are inferred from the")
        lines.append("book crossing the resting price, never from observed trades -- the")
        lines.append("archive has no trade feed -- so they are an upper bound. Queue position")
        lines.append("is why only the improved price is scored: joining the best bid would")
        lines.append("report fills behind size this system cannot see traded.")
        return "\n".join(lines)


def _best(levels: dict[str, str]) -> tuple[Decimal, Decimal] | None:
    if not levels:
        return None
    price = max(Decimal(p) for p in levels)
    return price, Decimal(levels[str(price)])


def scan_maker_capacity(
    session: Session,
    *,
    since: dt.datetime | None = None,
    event: str | None = None,
    max_leg_age: dt.timedelta = dt.timedelta(seconds=2),
    horizon: dt.timedelta = DEFAULT_FILL_HORIZON,
) -> MakerReport:
    """Replay the archive as a market maker rather than a taker."""
    stmt = select(
        BookSnapshot.ticker,
        BookSnapshot.captured_ts,
        BookSnapshot.yes_levels,
        BookSnapshot.no_levels,
    ).where(BookSnapshot.is_complete)
    if since is not None:
        stmt = stmt.where(BookSnapshot.captured_ts >= since)
    if event is not None:
        stmt = stmt.where(BookSnapshot.ticker.startswith(f"{event}-"))
    rows = session.execute(stmt.order_by(BookSnapshot.captured_ts, BookSnapshot.id)).all()

    legs_by_event: dict[str, set[str]] = defaultdict(set)
    for ticker, *_ in rows:
        legs_by_event[event_of(ticker)].add(ticker)

    # The forward view a fill test needs: every later ask on each leg, in time
    # order. Built once rather than re-scanned per moment, because the naive
    # version is quadratic and this archive is tens of thousands of rows.
    ask_series: dict[str, list[tuple[dt.datetime, Decimal]]] = defaultdict(list)
    for ticker, at, _yes, no_levels in rows:
        best_no = _best(no_levels or {})
        if best_no is not None:
            ask_series[ticker].append((at, PAYOUT_DOLLARS - best_no[0]))
    ask_times = {t: [when for when, _ in series] for t, series in ask_series.items()}

    def fills_by(ticker: str, price: Decimal, start: dt.datetime, deadline: dt.datetime) -> bool:
        """Whether a resting bid at ``price`` would have traded before ``deadline``.

        Inferred from the book, not observed: a seller offering at or below a
        standing bid is a crossed book and cannot persist, so the ask reaching
        the bid means the bid was hit.
        """
        series = ask_series.get(ticker)
        if not series:
            return False
        index = bisect_right(ask_times[ticker], start)
        for when, ask in series[index:]:
            if when > deadline:
                return False
            if ask <= price:
                return True
        return False

    def moved_against(ticker: str, price: Decimal, start: dt.datetime) -> bool | None:
        """Whether the leg kept falling after the fill -- the order was picked off."""
        series = ask_series.get(ticker)
        if not series:
            return None
        deadline = start + ADVERSE_SELECTION_HORIZON
        later = [
            ask
            for when, ask in series[bisect_right(ask_times[ticker], start) :]
            if when <= deadline
        ]
        if not later:
            return None
        return later[-1] < price

    confirmations = ConfirmationIndex(session, since=since)
    report = MakerReport(events_seen=len(legs_by_event), horizon=horizon)
    latest: dict[str, LegQuote] = {}

    for ticker, at, yes_levels, no_levels in rows:
        best_yes = _best(yes_levels or {})
        best_no = _best(no_levels or {})
        if best_yes is None or best_no is None:
            # A leg with no resting bid cannot be joined and a leg with no offer
            # has no ask to derive. Either way there is no two-sided market to
            # quote against, and pretending otherwise invents both prices.
            latest.pop(ticker, None)
            continue
        latest[ticker] = LegQuote(
            ticker=ticker,
            bid=best_yes[0],
            ask=PAYOUT_DOLLARS - best_no[0],
            bid_size=best_yes[1],
            at=at,
        )

        legs = legs_by_event[event_of(ticker)]
        if len(legs) < 2 or not legs <= latest.keys():
            continue
        if any(
            at - confirmations.last_confirmed(leg, at, default=latest[leg].at) > max_leg_age
            for leg in legs
        ):
            continue

        snapshot = MakerSnapshot(
            event=event_of(ticker), at=at, legs=tuple(latest[leg] for leg in sorted(legs))
        )
        report.moments += 1
        report.passive_costs.append(snapshot.passive_cost)
        report.improved_costs.append(snapshot.improved_cost)
        report.aggressive_costs.append(snapshot.aggressive_cost)
        report.passive_below_payout += snapshot.passive_cost < PAYOUT_DOLLARS
        report.improved_below_payout += snapshot.improved_cost < PAYOUT_DOLLARS
        report.aggressive_below_payout += snapshot.aggressive_cost < PAYOUT_DOLLARS

        if snapshot.improved_cost >= PAYOUT_DOLLARS:
            # Quoting here is buying a dollar for more than a dollar. Counting
            # its fills would measure how often a bad quote gets hit, which is
            # a number that only ever looks good.
            continue

        report.baskets_attempted += 1
        deadline = at + horizon
        filled = 0
        for leg in snapshot.legs:
            report.legs_quoted += 1
            if not fills_by(leg.ticker, leg.improved_bid, at, deadline):
                continue
            filled += 1
            report.legs_filled += 1
            against = moved_against(leg.ticker, leg.improved_bid, at)
            if against is not None:
                report.adverse_scored += 1
                report.adverse_against += against
        if filled == len(snapshot.legs):
            report.baskets_completed += 1

    return report
