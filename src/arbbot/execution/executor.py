"""Acquiring a basket leg by leg, and dealing with it when that fails (FR-012).

The detector prices a basket as though every leg could be bought at once.
Nothing can. Between the first fill and the last, the position is directional
and the guaranteed payout does not exist yet -- and this module is the part of
the system that lives inside that window. The shadow executor has been
modelling it since M3; this is the same failure handled for real.

Everything here follows from one rule: **a partial basket is not a smaller
arbitrage, it is a bet nobody chose.** So the executor's job is not to acquire
legs, it is to reach a state where either the whole basket is held or none of
it is, and to be honest when it cannot.

Four consequences.

**Nothing is submitted without every gate having passed.** The build flag, the
runtime flag, an approved relationship, the risk gate, and a per-basket human
approval. They are checked here, in one place, rather than trusted to have been
checked by whoever called -- FR-016's whole point is that arming is explicit,
and a check that lives at the call site is a check that gets forgotten at the
next call site.

**Every leg carries a stable idempotency key.** Derived from the intent and the
ticker, so a retry of a leg is recognisably the same order. Retrying without
one is how a hedged basket becomes a directional position, which is the most
expensive mistake available.

**An uncertain response stops everything immediately.** Not the leg -- the
whole intent, and the strategy with it. ``UNKNOWN`` means the venue may or may
not have filled, so continuing to buy the remaining legs risks completing a
basket on top of a position that may not exist, and unwinding risks selling
something that was never bought. The only correct next step is reconciliation
by a human, and the state machine already enforces that.

**Unwinding is the default response to failure, not hedging.** Chasing the
missing leg means paying whatever it now costs, on a market that just moved
against the assumption the basket was priced under. Getting out is the
conservative direction, and it is what the risk model assumes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from arbbot import buildflags
from arbbot.execution.gateway import OrderGateway, OrderOutcome, OrderRequest, OrderResult
from arbbot.money import ZERO, quantize_cost
from arbbot.reasons import RejectionReason
from arbbot.risk import ExposureSnapshot, RiskGate
from arbbot.states import OrderState, assert_transition

__all__ = ["BasketIntent", "ExecutionJournal", "ExecutionResult", "Executor", "leg_key"]


def leg_key(intent_id: str, ticker: str) -> str:
    """A stable idempotency key for one leg of one intent.

    Hashed rather than concatenated so a ticker containing the separator cannot
    collide with a different intent, and stable across retries and restarts so
    a resumed executor recognises its own in-flight orders rather than placing
    them again.
    """
    digest = hashlib.sha256(f"{intent_id}\x00{ticker}".encode()).hexdigest()
    return f"arbbot-{digest[:32]}"


@dataclass(frozen=True, slots=True)
class BasketIntent:
    """One approved basket, priced and ready to acquire."""

    intent_id: str
    legs: tuple[tuple[str, Decimal], ...]
    """``(ticker, limit_price)`` per leg, in acquisition order."""

    quantity: Decimal
    net_edge: Decimal
    quote_age: dt.timedelta | None
    relationship_approved: bool
    human_approved: bool = False
    """FR-016's third gate. Per basket, never per session: an approval that
    covered 'whatever the bot does next' is not an approval of anything."""

    @property
    def notional(self) -> Decimal:
        return quantize_cost(sum((price for _, price in self.legs), ZERO) * self.quantity)


@dataclass(slots=True)
class ExecutionResult:
    """What the executor did, and what it left behind."""

    intent_id: str
    state: OrderState
    reason: RejectionReason | None = None
    detail: str = ""

    acquired: dict[str, Decimal] = field(default_factory=dict)
    spent: Decimal = ZERO
    unwound: dict[str, Decimal] = field(default_factory=dict)
    recovered: Decimal = ZERO
    results: list[OrderResult] = field(default_factory=list)

    @property
    def realised(self) -> Decimal:
        """Money out, less money back. Negative is a loss."""
        return self.recovered - self.spent

    @property
    def needs_human(self) -> bool:
        return self.state in (OrderState.UNKNOWN, OrderState.INCIDENT)


class ExecutionJournal(Protocol):
    """Where an executor records what it is about to do, before doing it.

    A protocol so the executor can be tested without a database, and so the
    ordering requirement is visible in the type rather than buried in a
    persistence layer: ``opened`` is called before the first order is sent,
    ``leg`` as each one resolves, ``ended`` last. A journal called only at the
    end would be missing for exactly the runs that crashed mid-acquisition,
    which are the ones with real positions left at the venue.
    """

    def opened(self, intent: BasketIntent) -> None: ...

    def leg(
        self, intent_id: str, request: OrderRequest, result: OrderResult, side: str
    ) -> None: ...

    def ended(self, result: ExecutionResult) -> None: ...


class Executor:
    """Acquires approved baskets, or gets out of them."""

    def __init__(
        self,
        gateway: OrderGateway,
        risk: RiskGate,
        *,
        journal: ExecutionJournal | None = None,
    ) -> None:
        self._gateway = gateway
        self._risk = risk
        self._journal = journal

    def _record_leg(
        self, intent_id: str, request: OrderRequest, result: OrderResult, side: str
    ) -> None:
        if self._journal is not None:
            self._journal.leg(intent_id, request, result, side)

    def _refuse(
        self, intent: BasketIntent, reason: RejectionReason, detail: str
    ) -> ExecutionResult:
        """Every pre-submission refusal lands in the same terminal state.

        The reason distinguishes them; the state does not need to. What matters
        is that a refused intent can never transition onward -- the machine
        treats RISK_REJECTED as terminal, so a refusal cannot be walked back
        into a submission by later code.
        """
        assert_transition(OrderState.PROPOSED, OrderState.RISK_REJECTED)
        return ExecutionResult(
            intent.intent_id, OrderState.RISK_REJECTED, reason=reason, detail=detail
        )

    async def acquire(
        self, intent: BasketIntent, *, exposure: ExposureSnapshot | None = None
    ) -> ExecutionResult:
        """Take a basket, or refuse, or fail safely.

        A thin wrapper so ``ended`` is journalled exactly once, on every path
        out. Scattering that call across each return is how one branch ends up
        missing it, and the branch that gets missed is always an unusual one --
        which is exactly the intent whose record someone will later need.
        """
        result = await self._acquire(intent, exposure=exposure)
        if self._journal is not None:
            self._journal.ended(result)
        return result

    async def _acquire(
        self, intent: BasketIntent, *, exposure: ExposureSnapshot | None = None
    ) -> ExecutionResult:
        """The decision and acquisition itself.

        The gates are checked here rather than trusted to the caller. A gate
        enforced at the call site is a gate that gets forgotten at the next one.
        """
        # The build flag first, because it is the one that cannot be flipped by
        # configuration. A deployment without the execution path compiled in
        # must refuse before it evaluates anything else, so that a
        # misconfiguration cannot even look like it nearly traded.
        if not buildflags.LIVE_EXECUTION_COMPILED_IN and not buildflags.DEMO_EXECUTION_COMPILED_IN:
            return self._refuse(
                intent,
                RejectionReason.RISK_LIMIT,
                "no execution path is compiled into this build (FR-016); "
                "nothing here can reach a venue",
            )
        if not intent.relationship_approved:
            return self._refuse(
                intent,
                RejectionReason.RELATIONSHIP_NOT_APPROVED,
                "no approved relationship covers this leg set",
            )
        if not intent.human_approved:
            return self._refuse(
                intent,
                RejectionReason.RISK_LIMIT,
                "per-basket human approval is required and was not given (FR-016)",
            )

        decision = self._risk.evaluate(
            notional=intent.notional,
            net_edge=intent.net_edge,
            quote_age=intent.quote_age,
            exposure=exposure or ExposureSnapshot(),
        )
        if not decision.allowed:
            return self._refuse(
                intent, decision.reason or RejectionReason.RISK_LIMIT, decision.detail
            )

        result = ExecutionResult(intent.intent_id, OrderState.RISK_APPROVED)
        assert_transition(result.state, OrderState.SUBMITTING)
        result.state = OrderState.SUBMITTING

        # Recorded before the first order leaves. Everything past this line can
        # leave a real position at the venue, and a crash between here and the
        # first response must still be visible to reconciliation.
        if self._journal is not None:
            self._journal.opened(intent)

        for ticker, limit in intent.legs:
            request = OrderRequest(
                idempotency_key=leg_key(intent.intent_id, ticker),
                ticker=ticker,
                quantity=intent.quantity,
                limit_price=limit,
            )
            outcome = await self._gateway.place(request)
            result.results.append(outcome)
            self._record_leg(intent.intent_id, request, outcome, "buy")

            if outcome.outcome is OrderOutcome.UNKNOWN:
                # Stop the whole intent, not just this leg. Buying on is a
                # basket built on a position that may not exist; unwinding is
                # selling something that may never have been bought. Only a
                # human can tell which, and the machine refuses to leave
                # UNKNOWN for anything but reconciliation.
                assert_transition(result.state, OrderState.UNKNOWN)
                result.state = OrderState.UNKNOWN
                result.reason = RejectionReason.ORDER_STATE_UNKNOWN
                result.detail = f"{ticker}: {outcome.detail or 'no usable response'}"
                return result

            if outcome.filled > ZERO:
                result.acquired[ticker] = outcome.filled
                result.spent += outcome.cost

            if outcome.filled < intent.quantity:
                result.detail = f"{ticker}: filled {outcome.filled} of {intent.quantity}"
                return await self._unwind(intent, result)

        assert_transition(result.state, OrderState.FILLED)
        result.state = OrderState.FILLED
        return result

    async def _unwind(self, intent: BasketIntent, result: ExecutionResult) -> ExecutionResult:
        """Sell back whatever was acquired, because the basket cannot be held.

        Unwinding rather than hedging: chasing the missing leg means paying
        whatever it costs on a market that has just moved against the
        assumption the basket was priced under. Getting out is the conservative
        direction and the one the risk model assumes.
        """
        assert_transition(result.state, OrderState.PARTIAL)
        result.state = OrderState.PARTIAL

        if not result.acquired:
            # Nothing was taken, so there is nothing to sell. A failure with no
            # position is a failure, not an incident.
            assert_transition(result.state, OrderState.FAILED)
            result.state = OrderState.FAILED
            result.reason = RejectionReason.INSUFFICIENT_DEPTH
            return result

        assert_transition(result.state, OrderState.UNWINDING)
        result.state = OrderState.UNWINDING
        limits = dict(intent.legs)

        for ticker, held in list(result.acquired.items()):
            request = OrderRequest(
                idempotency_key=f"{leg_key(intent.intent_id, ticker)}-unwind",
                ticker=ticker,
                quantity=held,
                limit_price=limits[ticker],
            )
            sale = await self._gateway.sell(request)
            result.results.append(sale)
            self._record_leg(intent.intent_id, request, sale, "sell")

            if sale.outcome is OrderOutcome.UNKNOWN:
                assert_transition(result.state, OrderState.UNKNOWN)
                result.state = OrderState.UNKNOWN
                result.reason = RejectionReason.ORDER_STATE_UNKNOWN
                result.detail = f"unwind of {ticker} left the position uncertain"
                return result

            result.unwound[ticker] = sale.filled
            result.recovered += sale.cost

        assert_transition(result.state, OrderState.FAILED)
        result.state = OrderState.FAILED
        result.reason = RejectionReason.INSUFFICIENT_DEPTH
        return result
