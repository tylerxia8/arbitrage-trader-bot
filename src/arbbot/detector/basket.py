"""Qualification of exhaustive baskets (FR-006, FR-009, section 20).

Implements the specification's pricing algorithm in order: verify the
relationship is approved and its terms unchanged, verify every book is
complete and fresh, walk depth for the requested quantity, compute the
guaranteed minimum payout, deduct exact fees and the reserves, and reject
unless what remains clears the threshold.

Every path out of here is either an acceptance or a coded rejection, and both
are returned rather than raised. A rejection is evidence -- "nothing qualified
today" is only a useful sentence if it comes with a countable reason -- and
the daily falsification report is built from these.

Two properties are deliberate and worth stating, because both cost candidates:

**Every deduction rounds against the candidate.** Costs and fees round up,
payout rounds down. The reported edge is a lower bound on the true edge, never
an upper one, so a candidate cannot be accepted on a rounding artifact.

**The guaranteed payout is the minimum over states, not the expected one.**
For a partition exactly one leg pays, so the minimum is one dollar per basket.
That is the entire basis of calling this arbitrage rather than a bet, and it
holds only if the relationship's claim is true -- which is why nothing gets
here without a human approval behind it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from arbbot.economics.depth import DepthWalk, walk_levels
from arbbot.fees import FeeSchedule, UnknownFeeError, UnverifiedFeeError
from arbbot.marketdata.book import BookIntegrityError, OrderBook
from arbbot.marketdata.types import BookSide
from arbbot.money import PAYOUT_DOLLARS, ZERO, quantize_cost, quantize_proceeds
from arbbot.reasons import RejectionReason

__all__ = ["BasketEvaluation", "LegQuote", "evaluate_basket"]


@dataclass(frozen=True, slots=True)
class LegQuote:
    """One leg as priced for the requested quantity."""

    ticker: str
    side: BookSide
    walk: DepthWalk
    fee: Decimal


@dataclass(frozen=True, slots=True)
class BasketEvaluation:
    """The outcome of pricing one basket at one quantity.

    Accepted or rejected, this is persisted whole: every input, every
    deduction, and the reason code (FR-011, FR-018). A decision that cannot be
    re-derived from its own record is not auditable.
    """

    accepted: bool
    quantity: Decimal
    reason: RejectionReason | None = None
    detail: str = ""

    acquisition_cost: Decimal = ZERO
    fees: Decimal = ZERO
    reserves: Decimal = ZERO
    guaranteed_payout: Decimal = ZERO
    legs: tuple[LegQuote, ...] = ()
    evaluated_ts: dt.datetime | None = None

    @property
    def total_cost(self) -> Decimal:
        """Everything paid or set aside before the payout arrives."""
        return self.acquisition_cost + self.fees + self.reserves

    @property
    def net_edge(self) -> Decimal:
        """Guaranteed payout less every cost. A lower bound, by construction."""
        return self.guaranteed_payout - self.total_cost

    @classmethod
    def rejected(
        cls,
        reason: RejectionReason,
        detail: str,
        quantity: Decimal,
        **extra: object,
    ) -> BasketEvaluation:
        return cls(accepted=False, quantity=quantity, reason=reason, detail=detail, **extra)  # type: ignore[arg-type]


@dataclass(slots=True)
class BasketRequest:
    """Everything needed to price one basket."""

    books: dict[str, OrderBook]
    quantity: Decimal
    fees: FeeSchedule
    min_net_edge: Decimal
    book_ages: dict[str, dt.timedelta] = field(default_factory=dict)
    max_book_age: dt.timedelta = dt.timedelta(seconds=2)
    slippage_reserve: Decimal = ZERO
    latency_reserve: Decimal = ZERO
    safety_reserve: Decimal = ZERO
    require_verified_fees: bool = True


def evaluate_basket(request: BasketRequest, *, now: dt.datetime | None = None) -> BasketEvaluation:
    """Price an exhaustive basket and decide whether it qualifies.

    ``request.books`` must be the *complete* leg set of an approved
    relationship. This function does not check approval -- the registry does,
    and calling here without that check is the mistake the registry exists to
    prevent.
    """
    quantity = request.quantity
    if quantity <= ZERO:
        return BasketEvaluation.rejected(
            RejectionReason.INSUFFICIENT_DEPTH, "quantity must be positive", quantity
        )
    if len(request.books) < 2:
        return BasketEvaluation.rejected(
            RejectionReason.RELATIONSHIP_NOT_APPROVED,
            "a basket needs at least two legs",
            quantity,
        )

    legs: list[LegQuote] = []
    acquisition = ZERO
    fees_total = ZERO

    for ticker, book in request.books.items():
        if not book.is_complete:
            return BasketEvaluation.rejected(
                RejectionReason.BOOK_INCOMPLETE, f"{ticker}: book incomplete", quantity
            )

        age = request.book_ages.get(ticker)
        if age is None or age > request.max_book_age:
            # No age is treated as stale, not as fresh. An evaluation that
            # cannot say how old its quote was has not measured anything.
            return BasketEvaluation.rejected(
                RejectionReason.STALE_QUOTE,
                f"{ticker}: quote age {age if age is not None else 'unknown'} "
                f"exceeds {request.max_book_age}",
                quantity,
            )

        try:
            levels = book.ask_levels(BookSide.YES)
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
        except UnknownFeeError as exc:
            return BasketEvaluation.rejected(RejectionReason.UNKNOWN_FEE, str(exc), quantity)
        except UnverifiedFeeError as exc:
            # An unconfirmed fee rule is an unknown fee. Treating it as known
            # would let a candidate qualify on a number nobody has checked.
            return BasketEvaluation.rejected(RejectionReason.UNKNOWN_FEE, str(exc), quantity)

        acquisition += walk.cost
        fees_total += fee
        legs.append(LegQuote(ticker=ticker, side=BookSide.YES, walk=walk, fee=fee))

    # Exactly one leg of a true partition pays, so the guaranteed minimum is
    # one dollar per basket. Rounded down, like every other proceed.
    payout = quantize_proceeds(PAYOUT_DOLLARS * quantity)
    reserves = quantize_cost(
        (request.slippage_reserve + request.latency_reserve + request.safety_reserve) * quantity
    )

    evaluation = BasketEvaluation(
        accepted=False,
        quantity=quantity,
        acquisition_cost=quantize_cost(acquisition),
        fees=fees_total,
        reserves=reserves,
        guaranteed_payout=payout,
        legs=tuple(legs),
        evaluated_ts=now,
    )

    if evaluation.net_edge <= request.min_net_edge:
        return BasketEvaluation(
            accepted=False,
            quantity=quantity,
            reason=RejectionReason.NONPOSITIVE_NET_EDGE,
            detail=(f"net {evaluation.net_edge} does not exceed threshold {request.min_net_edge}"),
            acquisition_cost=evaluation.acquisition_cost,
            fees=evaluation.fees,
            reserves=evaluation.reserves,
            guaranteed_payout=evaluation.guaranteed_payout,
            legs=evaluation.legs,
            evaluated_ts=now,
        )

    return BasketEvaluation(
        accepted=True,
        quantity=quantity,
        acquisition_cost=evaluation.acquisition_cost,
        fees=evaluation.fees,
        reserves=evaluation.reserves,
        guaranteed_payout=evaluation.guaranteed_payout,
        legs=evaluation.legs,
        evaluated_ts=now,
    )
