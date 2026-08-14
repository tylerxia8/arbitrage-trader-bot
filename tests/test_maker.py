"""Replaying the archive as a maker rather than a taker.

A maker backtest is unusually easy to fool yourself with: fills are inferred
rather than observed, queue position is invisible, and the fills you do get
arrive precisely when the price is about to move against you. Every test here
pins one of the places this module refuses to flatter itself.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from arbbot.analysis.maker import scan_maker_capacity
from arbbot.db.models import BookSnapshot

D = Decimal
T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)
EVENT = "KXHIGHTEST-26AUG14"


def quote(
    session: Session,
    leg: str,
    *,
    bid: str | None,
    ask: str | None,
    at: dt.datetime = T0,
    size: str = "1000",
) -> None:
    """Store a two-sided book. ``ask`` is stored as the opposite side's NO bid."""
    session.add(
        BookSnapshot(
            venue="kalshi",
            ticker=f"{EVENT}-{leg}",
            captured_ts=at,
            sequence=1,
            yes_levels={} if bid is None else {f"{D(bid):.4f}": size},
            no_levels={} if ask is None else {f"{D('1.00') - D(ask):.4f}": size},
            checksum="x" * 64,
            is_complete=True,
        )
    )
    session.flush()


def basket(
    session: Session,
    bid: str,
    ask: str,
    *,
    at: dt.datetime = T0,
    legs: tuple[str, ...] = ("A", "B"),
) -> None:
    for leg in legs:
        quote(session, leg, bid=bid, ask=ask, at=at)


class TestCost:
    def test_the_three_routes_are_priced_separately(self, session: Session) -> None:
        """The spread is the whole point: crossing it is what made the taker
        path lose money, and the bid is what a maker would pay instead."""
        basket(session, bid="0.45", ask="0.55")
        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))

        assert report.moments == 1
        assert report.passive_costs == [D("0.90")]
        assert report.aggressive_costs == [D("1.10")]

    def test_queue_priority_costs_a_tick_per_leg(self, session: Session) -> None:
        basket(session, bid="0.45", ask="0.55")
        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))
        assert report.improved_costs == [D("0.9002")]

    def test_a_leg_with_no_resting_bid_is_not_quotable(self, session: Session) -> None:
        """There is nothing to join and no two-sided market. Inventing a bid
        would invent both the price and the queue position."""
        quote(session, "A", bid="0.45", ask="0.55")
        quote(session, "B", bid=None, ask="0.55")

        assert scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5)).moments == 0


class TestFills:
    def test_a_resting_bid_fills_when_the_ask_reaches_it(self, session: Session) -> None:
        """Inferred, not observed: a seller offering at or below a standing bid
        is a crossed book and cannot persist."""
        basket(session, bid="0.45", ask="0.55")
        basket(session, bid="0.30", ask="0.45", at=T0 + dt.timedelta(seconds=30))

        report = scan_maker_capacity(
            session, max_leg_age=dt.timedelta(minutes=5), horizon=dt.timedelta(minutes=2)
        )
        assert report.legs_filled == 2
        assert report.baskets_completed == 1

    def test_an_ask_that_never_reaches_the_bid_does_not_fill(self, session: Session) -> None:
        basket(session, bid="0.45", ask="0.55")
        basket(session, bid="0.45", ask="0.54", at=T0 + dt.timedelta(seconds=30))

        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))
        assert report.legs_filled == 0

    def test_a_fill_after_the_horizon_does_not_count(self, session: Session) -> None:
        """A basket assembled over ten minutes is six directional bets acquired
        at different times, not an arbitrage."""
        basket(session, bid="0.45", ask="0.55")
        basket(session, bid="0.30", ask="0.40", at=T0 + dt.timedelta(minutes=10))

        report = scan_maker_capacity(
            session, max_leg_age=dt.timedelta(minutes=30), horizon=dt.timedelta(minutes=2)
        )
        assert report.legs_filled == 0

    def test_a_partial_basket_is_not_completed(self, session: Session) -> None:
        """The failure mode that decides this whole strategy. A maker controls
        neither which legs fill nor when, and a partial basket is a directional
        position nobody chose."""
        quote(session, "A", bid="0.45", ask="0.55")
        quote(session, "B", bid="0.45", ask="0.55")
        later = T0 + dt.timedelta(seconds=30)
        quote(session, "A", bid="0.30", ask="0.40", at=later)
        quote(session, "B", bid="0.45", ask="0.55", at=later)

        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))
        assert report.legs_filled == 1
        assert report.baskets_completed == 0

    def test_a_fill_before_the_quote_does_not_count(self, session: Session) -> None:
        """Only the forward view. A cheap print from before the order existed
        is not a fill it could have received."""
        basket(session, bid="0.20", ask="0.30")
        basket(session, bid="0.45", ask="0.55", at=T0 + dt.timedelta(seconds=30))

        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))
        # The second moment quotes at 0.4502 and nothing later reaches it.
        assert report.baskets_completed == 0


class TestQuoteQuality:
    def test_a_basket_that_costs_more_than_a_dollar_is_not_attempted(
        self, session: Session
    ) -> None:
        """Quoting there is buying a dollar for more than a dollar. Counting
        its fills would measure how often a bad quote gets hit, which is a
        number that only ever looks good."""
        basket(session, bid="0.60", ask="0.70")
        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))

        assert report.moments == 1
        assert report.baskets_attempted == 0
        assert report.legs_quoted == 0


class TestAdverseSelection:
    def test_a_fill_the_market_ran_away_from_is_flagged(self, session: Session) -> None:
        """The cost that replaces fees. A resting bid fills exactly when
        somebody wants to sell, which is exactly when the price is about to
        fall -- and a fill rate reported without this is how a maker backtest
        talks itself into a business."""
        basket(session, bid="0.45", ask="0.55")
        basket(session, bid="0.30", ask="0.40", at=T0 + dt.timedelta(seconds=30))
        basket(session, bid="0.10", ask="0.20", at=T0 + dt.timedelta(minutes=3))

        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))
        assert report.adverse_scored == 2
        assert report.adverse_against == 2

    def test_a_fill_the_market_recovered_from_is_not(self, session: Session) -> None:
        basket(session, bid="0.45", ask="0.55")
        basket(session, bid="0.30", ask="0.40", at=T0 + dt.timedelta(seconds=30))
        basket(session, bid="0.70", ask="0.80", at=T0 + dt.timedelta(minutes=3))

        report = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5))
        assert report.adverse_against == 0


class TestReport:
    def test_the_report_states_that_fills_are_inferred(self, session: Session) -> None:
        basket(session, bid="0.45", ask="0.55")
        rendered = scan_maker_capacity(session, max_leg_age=dt.timedelta(minutes=5)).render()

        assert "inferred" in rendered
        assert "upper bound" in rendered
        assert "Queue position" in rendered

    def test_an_archive_with_no_two_sided_market_says_so(self, session: Session) -> None:
        quote(session, "A", bid=None, ask="0.55")
        quote(session, "B", bid=None, ask="0.55")
        assert "Nothing to quote against" in scan_maker_capacity(session).render()
