"""Pricing a cross-venue pair.

The subtraction is trivial. Every test here is about the numbers around it that
decide whether the subtraction means anything: the calendar, the fee that is
known, the fee that is not, and the approval that has not happened.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from arbbot.analysis.crossvenue_pricing import (
    CrossVenueQuote,
    CrossVenueReport,
    price_pair,
)

D = Decimal
NOW = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)


def quote(
    *,
    kalshi_ask: str = "0.096",
    other_bid: str = "0.129",
    quantity: str = "100",
    resolves: dt.datetime | None = NOW + dt.timedelta(days=365),
    approved: bool = False,
) -> CrossVenueQuote:
    built = price_pair(
        slug="xvenue:K~polymarket:1",
        kalshi_ticker="KXPRESPERSON-28-AOCA",
        other_venue="polymarket",
        other_id="1",
        question="AOC wins the 2028 presidential election",
        kalshi_yes_ask=D(kalshi_ask),
        other_yes_bid=D(other_bid),
        quantity=D(quantity),
        resolves=resolves,
        approved=approved,
    )
    assert built is not None
    return built


class TestPricing:
    def test_the_no_leg_is_the_complement_of_the_yes_bid(self) -> None:
        """The two outcome tokens are complements: an offer to buy YES at 0.129
        is an offer to sell NO at 0.871."""
        q = quote()
        assert q.other_no_ask == D("0.871")
        assert q.cost == D("0.967")

    def test_gross_edge_is_the_shortfall_from_a_dollar(self) -> None:
        q = quote()
        assert q.gross_edge == D("0.033")

    def test_a_missing_quote_on_either_side_is_not_a_cheap_pair(self) -> None:
        """Unbuyable and cheap are different things, and only one of them is
        an opportunity."""
        assert (
            price_pair(
                slug="s",
                kalshi_ticker="K",
                other_venue="v",
                other_id="1",
                question="q",
                kalshi_yes_ask=None,
                other_yes_bid=D("0.5"),
                quantity=D("1"),
                resolves=None,
            )
            is None
        )
        assert (
            price_pair(
                slug="s",
                kalshi_ticker="K",
                other_venue="v",
                other_id="1",
                question="q",
                kalshi_yes_ask=D("0.5"),
                other_yes_bid=None,
                quantity=D("1"),
                resolves=None,
            )
            is None
        )


class TestFees:
    def test_the_kalshi_fee_is_charged_and_confirmed(self) -> None:
        """A 9.6-cent leg at a hundred contracts: 0.07 x 100 x 0.096 x 0.904,
        rounded up to the cent."""
        q = quote()
        assert q.kalshi_fee == D("0.61")

    def test_net_edge_is_after_the_fee_that_is_known(self) -> None:
        q = quote()
        assert q.net_edge == D("2.69")

    def test_the_fee_can_exceed_a_thin_edge(self) -> None:
        """Which is the ordinary case, and why gross edge is not the headline."""
        q = quote(kalshi_ask="0.490", other_bid="0.505")
        assert q.gross_edge == D("0.015")
        assert q.net_edge < 0


class TestTheCalendar:
    def test_a_long_dated_pair_annualises_to_very_little(self) -> None:
        """The finding that changed how this report is ordered. Three cents on
        ninety-seven held two and a half years is under one percent a year."""
        rate = quote(resolves=NOW + dt.timedelta(days=880)).annualised(NOW)
        assert rate is not None
        assert rate < D("0.02")

    def test_the_same_edge_soon_is_a_different_business(self) -> None:
        near = quote(resolves=NOW + dt.timedelta(days=30))
        far = quote(resolves=NOW + dt.timedelta(days=880))
        assert near is not None
        assert far is not None
        assert near.annualised(NOW) > far.annualised(NOW) * 25  # type: ignore[operator]

    def test_a_very_short_horizon_reports_no_rate(self) -> None:
        """A one-cent edge over three days annualises to a number no capital
        can be paid: the fill is one-shot and nothing compounds at it."""
        q = quote(resolves=NOW + dt.timedelta(days=3))
        assert q.annualised(NOW) is None

    def test_an_unknown_resolution_date_reports_no_rate(self) -> None:
        q = quote(resolves=None)
        assert q.annualised(NOW) is None
        assert q.days_to_resolution(NOW) is None


class TestReport:
    def test_the_report_says_the_other_venue_fee_is_missing(self) -> None:
        report = CrossVenueReport(quotes=[quote()], priced_at=NOW)
        rendered = report.render()

        assert "upper bound" in rendered
        assert "has not verified" in rendered

    def test_unapproved_pairs_are_called_out(self) -> None:
        """A price is a guaranteed dollar only if both contracts settle on the
        same event, and no arithmetic here checks that."""
        rendered = CrossVenueReport(quotes=[quote()], priced_at=NOW).render()
        assert "no approved relationship" in rendered
        assert "a cheap row is a question" in rendered

    def test_nothing_credible_says_so_plainly(self) -> None:
        dear = quote(kalshi_ask="0.60", other_bid="0.30")
        rendered = CrossVenueReport(quotes=[dear], priced_at=NOW).render()
        assert "No pair both covers its Kalshi fee" in rendered

    def test_rows_are_ordered_by_rate_not_by_edge(self) -> None:
        """Absolute edge is what misled the first probe."""
        soon = quote(resolves=NOW + dt.timedelta(days=30))
        later = quote(resolves=NOW + dt.timedelta(days=880))
        rendered = CrossVenueReport(quotes=[later, soon], priced_at=NOW).render()

        assert rendered.index("30") < rendered.index("880")


class TestDivergence:
    """A wide gap between two venues is evidence of a mismatched claim.

    A short-dated sweep produced forty-three "positive" pairs led by one
    costing eighteen cents for a guaranteed dollar. It had matched Kalshi's
    national House-control market against Polymarket's IN-08 district market:
    both say Republican Party, House and win. The wider the gap, the better the
    arithmetic looks, so the worst pairs sort to the top of any report ordered
    by edge.
    """

    def test_the_national_versus_district_pairing_is_flagged(self) -> None:
        q = quote(kalshi_ask="0.15", other_bid="0.967", resolves=NOW + dt.timedelta(days=74))
        assert q.divergence > D("0.8")
        assert q.suspect is True

    def test_a_pair_within_a_spread_is_not_flagged(self) -> None:
        """Venues quoting the same event differ by a spread and a little noise."""
        q = quote(kalshi_ask="0.096", other_bid="0.129")
        assert q.divergence == D("0.033")
        assert q.suspect is False

    def test_mismatched_rows_are_separated_from_credible_ones(self) -> None:
        good = quote(kalshi_ask="0.096", other_bid="0.129")
        bad = quote(kalshi_ask="0.15", other_bid="0.967", resolves=NOW + dt.timedelta(days=74))
        report = CrossVenueReport(quotes=[bad, good], priced_at=NOW)

        assert report.mismatched == [bad]
        assert report.credible == [good]

    def test_a_mismatched_row_never_reaches_the_opportunity_table(self) -> None:
        """It would otherwise sort first, being the most attractive number."""
        bad = quote(kalshi_ask="0.15", other_bid="0.967", resolves=NOW + dt.timedelta(days=74))
        rendered = CrossVenueReport(quotes=[bad], priced_at=NOW).render()

        assert "PROBABLY NOT THE SAME CLAIM" in rendered
        assert "per year" not in rendered
        assert "national House control" in rendered
