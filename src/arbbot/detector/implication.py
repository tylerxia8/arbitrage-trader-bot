"""Qualification of implication pairs (FR-007, EPIC-8).

If A implies B, then holding NO on A and YES on B pays at least one dollar
whatever happens. The reasoning is a truth table over the states the world can
actually be in:

===========  ===========  ========  ========  =======
A            B            NO A      YES B     payout
===========  ===========  ========  ========  =======
yes          yes          $0        $1        $1
no           yes          $1        $1        $2
no           no           $1        $0        $1
yes          no           *impossible -- A implies B*
===========  ===========  ========  ========  =======

The minimum is one dollar, and the case that would pay nothing is exactly the
one the implication rules out. So the guarantee rests entirely on the
implication being *true*, and it fails silently if it is not: the portfolio
still looks fine, right up until the impossible state occurs and pays zero.

That is a sharper dependency than an exhaustive basket, where a missing
outcome at least tends to show up as a suspiciously cheap set. Here the price
looks ordinary and the risk is invisible, which is why this detector refuses
to enumerate the states itself. The approved relationship must carry the truth
table a human signed for, and this checks the prices against *that*, rather
than deriving a payout it would like to believe.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from arbbot.detector.basket import BasketEvaluation, LegQuote
from arbbot.economics.depth import walk_levels
from arbbot.fees import FeeSchedule, UnknownFeeError, UnverifiedFeeError
from arbbot.marketdata.book import BookIntegrityError, OrderBook
from arbbot.marketdata.types import BookSide
from arbbot.money import ZERO, quantize_cost, quantize_proceeds
from arbbot.reasons import RejectionReason

__all__ = ["ImplicationRequest", "evaluate_implication", "minimum_payout"]


@dataclass(slots=True)
class ImplicationRequest:
    """One implication portfolio, priced at one quantity."""

    antecedent: str
    """Ticker of A. Held as NO."""

    consequent: str
    """Ticker of B. Held as YES."""

    books: dict[str, OrderBook]
    quantity: Decimal
    fees: FeeSchedule
    min_net_edge: Decimal
    payout_table: tuple[tuple[str, str, str], ...]
    """The approved truth table: ``(a_state, b_state, payout_per_basket)``.

    Supplied by the relationship, not derived here. A detector that computes
    its own payout has assumed the very claim a reviewer was asked to check.
    """

    book_ages: dict[str, dt.timedelta] | None = None
    max_book_age: dt.timedelta = dt.timedelta(seconds=2)
    require_verified_fees: bool = True


def minimum_payout(payout_table: tuple[tuple[str, str, str], ...]) -> Decimal:
    """Least payout across every state the table enumerates.

    The minimum, never the expected value. An average would price this as a
    bet; the whole claim is that the worst case still pays.
    """
    if not payout_table:
        raise ValueError("an implication needs an enumerated payout table")
    return min(Decimal(payout) for _a, _b, payout in payout_table)


def evaluate_implication(
    request: ImplicationRequest, *, now: dt.datetime | None = None
) -> BasketEvaluation:
    """Price ``NO A + YES B`` and decide whether it qualifies."""
    quantity = request.quantity
    if quantity <= ZERO:
        return BasketEvaluation.rejected(
            RejectionReason.INSUFFICIENT_DEPTH, "quantity must be positive", quantity
        )

    try:
        guaranteed = minimum_payout(request.payout_table)
    except ValueError as exc:
        return BasketEvaluation.rejected(
            RejectionReason.RELATIONSHIP_NOT_APPROVED, str(exc), quantity
        )

    ages = request.book_ages or {}
    sides = ((request.antecedent, BookSide.NO), (request.consequent, BookSide.YES))

    legs: list[LegQuote] = []
    acquisition = ZERO
    fees_total = ZERO

    for ticker, side in sides:
        book = request.books.get(ticker)
        if book is None:
            return BasketEvaluation.rejected(
                RejectionReason.MARKET_NOT_OPEN, f"{ticker}: no book", quantity
            )
        if not book.is_complete:
            return BasketEvaluation.rejected(
                RejectionReason.BOOK_INCOMPLETE, f"{ticker}: book incomplete", quantity
            )

        age = ages.get(ticker)
        if age is None or age > request.max_book_age:
            return BasketEvaluation.rejected(
                RejectionReason.STALE_QUOTE,
                f"{ticker}: quote age {age if age is not None else 'unknown'} "
                f"exceeds {request.max_book_age}",
                quantity,
            )

        try:
            levels = book.ask_levels(side)
        except BookIntegrityError as exc:
            return BasketEvaluation.rejected(
                RejectionReason.BOOK_INCOMPLETE, f"{ticker}: {exc}", quantity
            )

        walk = walk_levels(levels, quantity)
        if not walk.is_complete:
            return BasketEvaluation.rejected(
                RejectionReason.INSUFFICIENT_DEPTH,
                f"{ticker}: only {walk.filled} of {quantity} available",
                quantity,
            )

        try:
            fee = request.fees.trade_fee(
                ticker,
                walk.average_price or ZERO,
                quantity,
                require_verified=request.require_verified_fees,
            )
        except (UnknownFeeError, UnverifiedFeeError) as exc:
            return BasketEvaluation.rejected(RejectionReason.UNKNOWN_FEE, str(exc), quantity)

        acquisition += walk.cost
        fees_total += fee
        legs.append(LegQuote(ticker=ticker, side=side, walk=walk, fee=fee))

    payout = quantize_proceeds(guaranteed * quantity)
    evaluation = BasketEvaluation(
        accepted=False,
        quantity=quantity,
        acquisition_cost=quantize_cost(acquisition),
        fees=fees_total,
        guaranteed_payout=payout,
        legs=tuple(legs),
        evaluated_ts=now,
    )

    if evaluation.net_edge <= request.min_net_edge:
        return BasketEvaluation(
            accepted=False,
            quantity=quantity,
            reason=RejectionReason.NONPOSITIVE_NET_EDGE,
            detail=f"net {evaluation.net_edge} does not exceed {request.min_net_edge}",
            acquisition_cost=evaluation.acquisition_cost,
            fees=evaluation.fees,
            guaranteed_payout=evaluation.guaranteed_payout,
            legs=evaluation.legs,
            evaluated_ts=now,
        )

    return BasketEvaluation(
        accepted=True,
        quantity=quantity,
        acquisition_cost=evaluation.acquisition_cost,
        fees=evaluation.fees,
        guaranteed_payout=evaluation.guaranteed_payout,
        legs=evaluation.legs,
        evaluated_ts=now,
    )
