"""Implication pairs (FR-007, EPIC-8).

If A implies B, holding NO A and YES B pays at least a dollar in every state
the implication permits. The state that would pay nothing -- A true and B
false -- is exactly the one the implication rules out, so the guarantee rests
entirely on the implication being true and fails *silently* if it is not.

That is sharper than an exhaustive basket, where a missing outcome tends to
show up as a suspiciously cheap set. Here the price looks ordinary and the
risk is invisible, which is why these tests care most that the detector uses
the approved truth table rather than one it derived for itself.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from arbbot.detector import ImplicationRequest, evaluate_implication, minimum_payout
from arbbot.fees import FeeRule, FeeSchedule
from arbbot.marketdata.book import OrderBook
from arbbot.marketdata.types import BookSide, PriceLevel
from arbbot.money import PAYOUT_DOLLARS
from arbbot.reasons import RejectionReason

D = Decimal
FRESH = dt.timedelta(milliseconds=500)

FREE = FeeSchedule(
    (
        FeeRule(
            name="free",
            rate=D("0"),
            source="test",
            effective_from=dt.date(2020, 1, 1),
            verified=True,
        ),
    )
)

#: The states an implication permits, and what the portfolio pays in each.
#: A-true/B-false is absent because the implication forbids it.
APPROVED_TABLE = (
    ("yes", "yes", "1.00"),
    ("no", "yes", "2.00"),
    ("no", "no", "1.00"),
)


def book_with(
    no_ask: str | None = None, yes_ask: str | None = None, size: str = "1000"
) -> OrderBook:
    """A book offering the given asks. The venue quotes bids on both sides."""
    levels = []
    if yes_ask is not None:
        levels.append((BookSide.NO, PriceLevel(PAYOUT_DOLLARS - D(yes_ask), D(size))))
    if no_ask is not None:
        levels.append((BookSide.YES, PriceLevel(PAYOUT_DOLLARS - D(no_ask), D(size))))
    book = OrderBook("TEST")
    book.apply_snapshot(levels, sequence=1)
    return book


def request(
    *,
    no_a: str,
    yes_b: str,
    quantity: str = "1",
    sizes: tuple[str, str] = ("1000", "1000"),
    table: tuple[tuple[str, str, str], ...] = APPROVED_TABLE,
    min_edge: str = "0",
) -> ImplicationRequest:
    books = {
        "A": book_with(no_ask=no_a, size=sizes[0]),
        "B": book_with(yes_ask=yes_b, size=sizes[1]),
    }
    return ImplicationRequest(
        antecedent="A",
        consequent="B",
        books=books,
        quantity=D(quantity),
        fees=FREE,
        min_net_edge=D(min_edge),
        payout_table=table,
        book_ages={"A": FRESH, "B": FRESH},
    )


class TestPayoutTable:
    def test_the_minimum_is_taken_not_the_average(self) -> None:
        """An average would price this as a bet. The whole claim is that the
        worst permitted state still pays."""
        assert minimum_payout(APPROVED_TABLE) == D("1.00")

    def test_an_empty_table_is_refused(self) -> None:
        with pytest.raises(ValueError, match="enumerated payout table"):
            minimum_payout(())

    def test_a_relationship_without_a_table_cannot_qualify(self) -> None:
        """The detector will not derive a payout it would like to believe."""
        evaluation = evaluate_implication(request(no_a="0.30", yes_b="0.30", table=()))
        assert not evaluation.accepted
        assert evaluation.reason is RejectionReason.RELATIONSHIP_NOT_APPROVED

    def test_the_approved_table_governs_the_payout(self) -> None:
        """The reviewer's enumeration decides, not the code's optimism.

        The same prices qualify against a table whose worst state pays a
        dollar and fail against one whose worst state pays fifty cents. If the
        detector derived its own payout it would accept both.
        """
        assert evaluate_implication(request(no_a="0.40", yes_b="0.40")).accepted

        weaker = (("yes", "yes", "0.50"), ("no", "yes", "2.00"), ("no", "no", "1.00"))
        refused = evaluate_implication(request(no_a="0.40", yes_b="0.40", table=weaker))
        assert not refused.accepted
        assert refused.guaranteed_payout == D("0.50")


class TestPricing:
    def test_a_cheap_pair_qualifies(self) -> None:
        evaluation = evaluate_implication(request(no_a="0.40", yes_b="0.40"))
        assert evaluation.accepted
        assert evaluation.acquisition_cost == D("0.80")
        assert evaluation.guaranteed_payout == D("1.00")
        assert evaluation.net_edge == D("0.20")

    def test_a_dear_pair_is_refused(self) -> None:
        evaluation = evaluate_implication(request(no_a="0.60", yes_b="0.60"))
        assert not evaluation.accepted
        assert evaluation.reason is RejectionReason.NONPOSITIVE_NET_EDGE

    def test_each_leg_uses_its_own_side(self) -> None:
        """A is held NO and B is held YES. Reading both as YES would price a
        completely different portfolio."""
        evaluation = evaluate_implication(request(no_a="0.10", yes_b="0.80"))
        sides = {leg.ticker: leg.side for leg in evaluation.legs}
        assert sides == {"A": BookSide.NO, "B": BookSide.YES}

    def test_payout_scales_with_quantity(self) -> None:
        evaluation = evaluate_implication(request(no_a="0.40", yes_b="0.40", quantity="5"))
        assert evaluation.guaranteed_payout == D("5.00")


class TestRejections:
    def test_a_missing_book_is_refused(self) -> None:
        req = request(no_a="0.40", yes_b="0.40")
        del req.books["B"]
        evaluation = evaluate_implication(req)
        assert evaluation.reason is RejectionReason.MARKET_NOT_OPEN

    def test_insufficient_depth_is_refused(self) -> None:
        evaluation = evaluate_implication(
            request(no_a="0.40", yes_b="0.40", quantity="10", sizes=("1000", "3"))
        )
        assert evaluation.reason is RejectionReason.INSUFFICIENT_DEPTH

    def test_a_stale_quote_is_refused(self) -> None:
        req = request(no_a="0.40", yes_b="0.40")
        req.book_ages = {"A": FRESH, "B": dt.timedelta(minutes=1)}
        assert evaluate_implication(req).reason is RejectionReason.STALE_QUOTE

    def test_an_unverified_fee_rule_is_refused(self) -> None:
        from arbbot.fees import KALSHI_2022_SCHEDULE

        req = request(no_a="0.40", yes_b="0.40")
        req.fees = KALSHI_2022_SCHEDULE
        assert evaluate_implication(req).reason is RejectionReason.UNKNOWN_FEE

    def test_an_incomplete_book_is_refused(self) -> None:
        req = request(no_a="0.40", yes_b="0.40")
        req.books["A"].invalidate()
        assert evaluate_implication(req).reason is RejectionReason.BOOK_INCOMPLETE

    @pytest.mark.parametrize("quantity", ["0", "-1"])
    def test_a_nonpositive_quantity_is_refused(self, quantity: str) -> None:
        assert not evaluate_implication(
            request(no_a="0.40", yes_b="0.40", quantity=quantity)
        ).accepted


class TestEvidence:
    def test_both_legs_are_recorded(self) -> None:
        evaluation = evaluate_implication(request(no_a="0.40", yes_b="0.40", quantity="3"))
        assert len(evaluation.legs) == 2
        assert all(leg.walk.is_complete for leg in evaluation.legs)
