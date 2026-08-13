"""Depth walking (FR-009).

The observed books put four contracts at the top of a leg and nothing behind
it for cents, so top-of-book pricing overstates what a basket can be filled
at. These tests pin the two refusals that matter: a book that cannot fill the
quantity says so, and nothing is ever assumed past the last level.
"""

from __future__ import annotations

from decimal import Decimal

from arbbot.economics.depth import walk_levels
from arbbot.marketdata.types import PriceLevel

D = Decimal


def levels(*pairs: tuple[str, str]) -> list[PriceLevel]:
    """Ascending ask levels, cheapest first."""
    return [PriceLevel(D(price), D(size)) for price, size in pairs]


class TestSingleLevel:
    def test_a_fill_inside_the_top_level(self) -> None:
        walk = walk_levels(levels(("0.40", "100")), D("10"))
        assert walk.is_complete
        assert walk.cost == D("4.00")
        assert walk.levels_used == 1

    def test_an_exact_fill(self) -> None:
        walk = walk_levels(levels(("0.40", "10")), D("10"))
        assert walk.is_complete
        assert walk.cost == D("4.00")


class TestWalking:
    def test_cost_climbs_through_levels(self) -> None:
        """The whole reason for walking: 10 at $0.40 plus 10 at $0.50 is
        $9.00, not the $8.00 top-of-book would suggest."""
        walk = walk_levels(levels(("0.40", "10"), ("0.50", "10")), D("20"))
        assert walk.is_complete
        assert walk.cost == D("9.00")
        assert walk.levels_used == 2

    def test_the_worst_price_is_reported(self) -> None:
        """What the last contract cost, which the average hides."""
        walk = walk_levels(levels(("0.40", "10"), ("0.90", "10")), D("20"))
        assert walk.worst_price == D("0.90")
        assert walk.average_price == D("0.65")

    def test_partial_consumption_of_a_level(self) -> None:
        walk = walk_levels(levels(("0.40", "10"), ("0.50", "100")), D("15"))
        assert walk.cost == D("6.50")

    def test_fractional_quantities(self) -> None:
        """Contract counts are fractional on this venue."""
        walk = walk_levels(levels(("0.40", "4.25")), D("4.25"))
        assert walk.is_complete
        assert walk.cost == D("1.70")


class TestInsufficientDepth:
    def test_a_short_book_does_not_complete(self) -> None:
        """Filling what is available and calling it a basket is how a
        multi-leg trade turns into a directional position."""
        walk = walk_levels(levels(("0.40", "4")), D("10"))
        assert not walk.is_complete
        assert walk.filled == D("4")
        assert walk.shortfall == D("6")

    def test_an_empty_book_fills_nothing(self) -> None:
        walk = walk_levels([], D("10"))
        assert not walk.is_complete
        assert walk.filled == D("0")
        assert walk.average_price is None

    def test_nothing_is_assumed_past_the_last_level(self) -> None:
        """A book that ends is a book that ends. The invented part is exactly
        the part that does not fill."""
        walk = walk_levels(levels(("0.40", "5"), ("0.45", "5")), D("100"))
        assert walk.filled == D("10")
        assert walk.cost == D("4.25")


class TestRounding:
    def test_cost_rounds_up(self) -> None:
        """A fraction of a cent that rounds in our favour is edge
        manufactured by arithmetic."""
        # 3 x 0.3333 = 0.9999, which must not become 0.99.
        walk = walk_levels(levels(("0.3333", "10")), D("3"))
        assert walk.cost == D("1.00")

    def test_zero_quantity_costs_nothing(self) -> None:
        walk = walk_levels(levels(("0.40", "10")), D("0"))
        assert walk.cost == D("0")
        assert walk.levels_used == 0

    def test_negative_quantity_is_refused(self) -> None:
        walk = walk_levels(levels(("0.40", "10")), D("-5"))
        assert walk.filled == D("0")


class TestAgainstRealBooks:
    def test_the_philadelphia_leg(self) -> None:
        """Four contracts at $0.79 and nothing behind them, from the archive.
        Asking for ten shows the shortfall that top-of-book pricing hid."""
        book = levels(("0.79", "4"))
        assert walk_levels(book, D("4")).is_complete
        assert not walk_levels(book, D("10")).is_complete
