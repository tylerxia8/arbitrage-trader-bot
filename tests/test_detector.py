"""Basket qualification (FR-006, FR-009, section 20).

Golden cases first: the specification's own acceptance scenarios, priced
through the real code. Then every rejection path, because "nothing qualified
today" is only useful if it comes with a countable reason.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from arbbot.detector import BasketRequest, evaluate_basket
from arbbot.fees import FeeRule, FeeSchedule
from arbbot.marketdata.book import OrderBook
from arbbot.marketdata.types import BookSide, PriceLevel
from arbbot.money import PAYOUT_DOLLARS
from arbbot.reasons import RejectionReason

D = Decimal
FRESH = dt.timedelta(milliseconds=500)

#: A verified copy of the venue rule, so tests exercise pricing rather than
#: the unverified-fee refusal (which has its own test below).
VERIFIED_FEES = FeeSchedule(
    (
        FeeRule(
            name="test-verified",
            rate=D("0.07"),
            source="test",
            effective_from=dt.date(2020, 1, 1),
            verified=True,
        ),
    )
)

FREE = FeeSchedule(
    (
        FeeRule(
            name="test-free",
            rate=D("0"),
            source="test",
            effective_from=dt.date(2020, 1, 1),
            verified=True,
        ),
    )
)


def book_at(ask: str, size: str = "1000") -> OrderBook:
    """A book whose cheapest YES ask is ``ask``.

    The venue quotes NO bids, so a YES ask of $0.30 is a NO bid at $0.70.
    """
    book = OrderBook("TEST")
    book.apply_snapshot([(BookSide.NO, PriceLevel(PAYOUT_DOLLARS - D(ask), D(size)))], sequence=1)
    return book


def request(
    asks: list[str],
    *,
    quantity: str = "1",
    sizes: list[str] | None = None,
    fees: FeeSchedule = FREE,
    min_edge: str = "0",
    **kw: object,
) -> BasketRequest:
    sizes = sizes or ["1000"] * len(asks)
    books = {
        f"LEG{i}": book_at(ask, size) for i, (ask, size) in enumerate(zip(asks, sizes, strict=True))
    }
    return BasketRequest(
        books=books,
        quantity=D(quantity),
        fees=fees,
        min_net_edge=D(min_edge),
        book_ages=dict.fromkeys(books, FRESH),
        **kw,  # type: ignore[arg-type]
    )


class TestAcceptance:
    def test_the_specifications_worked_example_prices_as_stated(self) -> None:
        """Section 25: "Three approved exhaustive outcomes cost $0.95 before
        $0.02 costs -- accepted at $0.03 net edge if depth and buffers pass."

        The acquisition side matches exactly.
        """
        evaluation = evaluate_basket(
            request(["0.30", "0.30", "0.35"], quantity="1", fees=VERIFIED_FEES)
        )
        assert evaluation.acquisition_cost == D("0.95")
        assert evaluation.guaranteed_payout == D("1.00")

    def test_the_specifications_example_is_refused_at_one_contract(self) -> None:
        """...but it does not qualify, because the costs are not $0.02.

        The venue's fee rounds up to a cent per leg, and near mid prices each
        of these three legs costs $0.02 -- $0.06 in total, three times what
        the example assumes. Against a five-cent edge that is a one-cent loss.

        Worth stating plainly: the specification's own illustrative acceptance
        does not survive the venue's own fee schedule at one contract.
        """
        evaluation = evaluate_basket(
            request(["0.30", "0.30", "0.35"], quantity="1", fees=VERIFIED_FEES)
        )
        assert evaluation.fees == D("0.06")
        assert evaluation.net_edge == D("-0.01")
        assert not evaluation.accepted
        assert evaluation.reason is RejectionReason.NONPOSITIVE_NET_EDGE

    def test_the_same_basket_qualifies_once_the_rounding_amortises(self) -> None:
        """At ten contracts the per-leg cent floor stops dominating and the
        proportional part of the fee is small enough to leave an edge.

        This is the one thing size *does* fix: the rounding floor, not the
        proportional fee.
        """
        evaluation = evaluate_basket(
            request(["0.30", "0.30", "0.35"], quantity="10", fees=VERIFIED_FEES)
        )
        assert evaluation.accepted
        assert evaluation.net_edge == D("0.04")

    def test_a_cheap_basket_qualifies(self) -> None:
        evaluation = evaluate_basket(request(["0.30", "0.30", "0.30"]))
        assert evaluation.accepted
        assert evaluation.net_edge == D("0.10")

    def test_payout_is_one_dollar_per_basket(self) -> None:
        """Exactly one leg of a true partition pays."""
        evaluation = evaluate_basket(request(["0.30", "0.30"], quantity="7"))
        assert evaluation.guaranteed_payout == D("7.00")


class TestRejections:
    def test_a_dear_basket_is_refused(self) -> None:
        evaluation = evaluate_basket(request(["0.40", "0.40", "0.40"]))
        assert not evaluation.accepted
        assert evaluation.reason is RejectionReason.NONPOSITIVE_NET_EDGE

    def test_an_edge_below_threshold_is_refused(self) -> None:
        evaluation = evaluate_basket(request(["0.49", "0.49"], min_edge="0.05"))
        assert not evaluation.accepted
        assert evaluation.reason is RejectionReason.NONPOSITIVE_NET_EDGE

    def test_insufficient_depth_is_refused(self) -> None:
        """FR-009. The Philadelphia shape: cheap, and four contracts deep."""
        evaluation = evaluate_basket(request(["0.30", "0.30"], quantity="10", sizes=["1000", "4"]))
        assert not evaluation.accepted
        assert evaluation.reason is RejectionReason.INSUFFICIENT_DEPTH
        assert "4" in evaluation.detail

    def test_an_incomplete_book_is_refused(self) -> None:
        req = request(["0.30", "0.30"])
        next(iter(req.books.values())).invalidate()
        evaluation = evaluate_basket(req)
        assert evaluation.reason is RejectionReason.BOOK_INCOMPLETE

    def test_a_stale_quote_is_refused(self) -> None:
        req = request(["0.30", "0.30"])
        req.book_ages = dict.fromkeys(req.books, dt.timedelta(seconds=30))
        evaluation = evaluate_basket(req)
        assert evaluation.reason is RejectionReason.STALE_QUOTE

    def test_an_unmeasured_quote_age_is_refused(self) -> None:
        """No age is stale, not fresh. An evaluation that cannot say how old
        its quote was has not measured anything."""
        req = request(["0.30", "0.30"])
        req.book_ages = {}
        evaluation = evaluate_basket(req)
        assert evaluation.reason is RejectionReason.STALE_QUOTE
        assert "unknown" in evaluation.detail

    def test_a_single_leg_is_not_a_basket(self) -> None:
        evaluation = evaluate_basket(request(["0.30"]))
        assert evaluation.reason is RejectionReason.RELATIONSHIP_NOT_APPROVED


class TestFees:
    def test_an_unverified_fee_rule_refuses_the_candidate(self) -> None:
        """FR-010. A fee nobody has confirmed is an unknown fee, and an
        unknown fee cannot produce an accepted candidate."""
        from arbbot.fees import KALSHI_2022_SCHEDULE

        evaluation = evaluate_basket(request(["0.30", "0.30"], fees=KALSHI_2022_SCHEDULE))
        assert not evaluation.accepted
        assert evaluation.reason is RejectionReason.UNKNOWN_FEE

    def test_an_absent_fee_rule_refuses_the_candidate(self) -> None:
        evaluation = evaluate_basket(request(["0.30", "0.30"], fees=FeeSchedule(())))
        assert evaluation.reason is RejectionReason.UNKNOWN_FEE

    def test_fees_are_deducted(self) -> None:
        free = evaluate_basket(request(["0.30", "0.30"], quantity="100"))
        charged = evaluate_basket(request(["0.30", "0.30"], quantity="100", fees=VERIFIED_FEES))
        assert charged.net_edge < free.net_edge

    def test_fees_can_turn_a_cheap_basket_into_a_loss(self) -> None:
        """The economics that decide this strategy: the fee is proportional to
        size, so a thin edge never survives however large the basket."""
        evaluation = evaluate_basket(
            request(["0.32", "0.32", "0.32"], quantity="900", fees=VERIFIED_FEES)
        )
        assert not evaluation.accepted
        assert evaluation.net_edge < 0


class TestReserves:
    def test_reserves_are_deducted_per_basket(self) -> None:
        req = request(["0.30", "0.30"], quantity="10")
        req.slippage_reserve = D("0.01")
        req.latency_reserve = D("0.01")
        evaluation = evaluate_basket(req)
        assert evaluation.reserves == D("0.20")

    def test_reserves_can_refuse_a_marginal_candidate(self) -> None:
        req = request(["0.48", "0.48"])
        req.safety_reserve = D("0.05")
        assert not evaluate_basket(req).accepted


class TestConservatism:
    def test_the_reported_edge_is_a_lower_bound(self) -> None:
        """Costs round up and payout rounds down, so a candidate can never be
        accepted on a rounding artifact.

        Rounding is applied **per leg**, not to the total, because that is how
        the venue settles it: each fill's balance change is floored separately.
        Three legs at $0.3333 therefore cost $1.02, not the $0.9999 the
        arithmetic suggests -- and a basket that looks a hundredth of a cent
        cheap is correctly refused rather than accepted on the difference.
        """
        evaluation = evaluate_basket(request(["0.3333", "0.3333", "0.3333"]))
        assert evaluation.acquisition_cost == D("1.02")
        assert not evaluation.accepted

    @pytest.mark.parametrize("quantity", ["0", "-5"])
    def test_a_nonpositive_quantity_is_refused(self, quantity: str) -> None:
        assert not evaluate_basket(request(["0.30", "0.30"], quantity=quantity)).accepted


class TestEvidence:
    def test_every_leg_is_recorded(self) -> None:
        """FR-018: a decision that cannot be re-derived from its own record is
        not auditable."""
        evaluation = evaluate_basket(request(["0.30", "0.30", "0.30"], quantity="5"))
        assert len(evaluation.legs) == 3
        assert all(leg.walk.is_complete for leg in evaluation.legs)

    def test_rejections_carry_a_reason_and_detail(self) -> None:
        evaluation = evaluate_basket(request(["0.30", "0.30"], quantity="10", sizes=["4", "1000"]))
        assert evaluation.reason is not None
        assert evaluation.detail
