"""Where the executor writes down what it is about to do, and what happened.

The risk gate sizes every candidate against what is currently open. Until now
"what is currently open" was whatever the caller passed in, which is not a
limit -- it is a limit-shaped argument. This is where that number actually
comes from.

**Writes happen before the venue is touched, not after it answers.** A process
that dies halfway through acquiring a basket has left real positions at a real
venue. A record written after the last leg returns would be missing for exactly
the intents that most need one, which is the opposite of useful. So the intent
row is committed before the first order is sent, and each leg is committed as
it resolves.

**Idempotency is enforced by the database, not by care.** ``leg_order``'s
unique key means a second insert under the same idempotency key raises rather
than quietly succeeding. Discipline is not a sufficient defence against the
most expensive bug available here -- a retried submit that fills twice, turning
one leg of a hedged basket into a directional position.

**Exposure is derived, never accumulated.** The snapshot is computed from the
rows each time it is asked for, rather than kept as a running total that could
drift from them. A cached number that disagrees with the ledger is worse than
no number, because it is confidently wrong.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.collection.health import utc_now
from arbbot.db.models import LegOrder, OrderIntent
from arbbot.execution.executor import BasketIntent, ExecutionResult
from arbbot.execution.gateway import OrderRequest, OrderResult
from arbbot.money import ZERO
from arbbot.risk import ExposureSnapshot, OpenIntent
from arbbot.states import OrderState, assert_transition, is_exposed

__all__ = ["ExecutionStore"]


class ExecutionStore:
    """Persists intents and legs, and reports what is currently at risk."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writing ---------------------------------------------------------
    def open_intent(
        self, intent: BasketIntent, *, relationship_slug: str, now: dt.datetime | None = None
    ) -> OrderIntent:
        """Record an intent before anything is submitted.

        Deliberately the first thing that happens. Everything after this point
        can leave a position behind, and a crash between here and the venue
        must still be visible to reconciliation.
        """
        row = OrderIntent(
            intent_id=intent.intent_id,
            relationship_slug=relationship_slug,
            state=OrderState.PROPOSED.value,
            quantity=intent.quantity,
            notional=intent.notional,
            net_edge=intent.net_edge,
            spent=ZERO,
            recovered=ZERO,
            created_ts=now or utc_now(),
            updated_ts=now or utc_now(),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def find(self, intent_id: str) -> OrderIntent | None:
        return self._session.execute(
            select(OrderIntent).where(OrderIntent.intent_id == intent_id)
        ).scalar_one_or_none()

    def transition(
        self,
        intent_id: str,
        target: OrderState,
        *,
        reason: str | None = None,
        detail: str | None = None,
        now: dt.datetime | None = None,
    ) -> OrderIntent:
        """Move an intent, refusing any transition the machine forbids.

        Checked here as well as in the executor. The state machine is the
        authority on what is legal, and a persistence layer that would happily
        write an impossible state makes the machine advisory.
        """
        row = self.find(intent_id)
        if row is None:
            raise KeyError(f"no intent {intent_id!r} to transition")
        assert_transition(OrderState(row.state), target)
        row.state = target.value
        if reason is not None:
            row.reason = reason
        if detail is not None:
            row.detail = detail
        row.updated_ts = now or utc_now()
        self._session.flush()
        return row

    def record_leg(
        self,
        intent_id: str,
        request: OrderRequest,
        result: OrderResult,
        *,
        side: str = "buy",
        now: dt.datetime | None = None,
    ) -> LegOrder:
        """Write one leg's outcome.

        Raises on a duplicate idempotency key, by database constraint. That is
        the intended behaviour: a second write under the same key means the
        same order was sent twice, and finding out loudly is the whole point.
        """
        row = self.find(intent_id)
        if row is None:
            raise KeyError(f"no intent {intent_id!r} to attach a leg to")

        leg = LegOrder(
            intent_row_id=row.id,
            idempotency_key=request.idempotency_key,
            ticker=request.ticker,
            side=side,
            limit_price=request.limit_price,
            quantity=request.quantity,
            filled=result.filled,
            cost=result.cost,
            outcome=result.outcome.value,
            venue_order_id=result.venue_order_id,
            detail=result.detail or None,
            submitted_ts=now or utc_now(),
        )
        self._session.add(leg)

        if side == "buy":
            row.spent = Decimal(row.spent) + result.cost
        else:
            row.recovered = Decimal(row.recovered) + result.cost
        row.updated_ts = now or utc_now()
        self._session.flush()
        return leg

    def finish(self, result: ExecutionResult, *, now: dt.datetime | None = None) -> OrderIntent:
        """Record where an execution ended up."""
        return self.transition(
            result.intent_id,
            result.state,
            reason=str(result.reason) if result.reason else None,
            detail=result.detail or None,
            now=now,
        )

    # -- reading ---------------------------------------------------------
    def exposure(
        self, *, realised_loss_today: Decimal = ZERO, include_pending: bool = False
    ) -> ExposureSnapshot:
        """What is currently at risk, computed from the rows.

        Derived rather than accumulated. A running total that drifts from the
        rows is worse than no total, because it is confidently wrong -- and the
        risk gate would then size the next basket against a number nothing
        supports.

        :param include_pending: also count baskets parked in ``AWAITING_HUMAN``.
            They hold nothing -- no order has been sent -- so the executor must
            *not* count them when deciding whether a submission fits. But the
            loop must, when deciding whether to park another one: a queue of
            sixty baskets that each fit individually and cannot all be taken is
            a queue that lies to whoever is reading it. The executor's own
            re-check is what keeps that safe; this is what keeps it honest.
        """
        rows = list(self._session.execute(select(OrderIntent)).scalars())
        intents: list[OpenIntent] = []
        for row in rows:
            state = OrderState(row.state)
            pending = include_pending and state is OrderState.AWAITING_HUMAN
            if not is_exposed(state) and not pending:
                continue
            # Unmatched is the whole notional until the basket is complete.
            # Between the first leg and the last there is no hedge at all, and
            # sizing this against the residual would understate precisely the
            # window that carries the risk.
            # A parked basket reserves capacity but is not unmatched: nothing
            # has been bought, so there is no directional position to carry.
            unmatched = (
                ZERO
                if state in (OrderState.FILLED, OrderState.AWAITING_HUMAN)
                else Decimal(row.notional)
            )
            intents.append(
                OpenIntent(
                    intent_id=row.intent_id,
                    state=state,
                    committed=Decimal(row.notional),
                    unmatched=unmatched,
                    reserved=pending,
                )
            )
        return ExposureSnapshot(intents=intents, realised_loss_today=realised_loss_today)

    def awaiting_human(self) -> list[OrderIntent]:
        """Intents parked waiting for a person to approve or decline them."""
        return list(
            self._session.execute(
                select(OrderIntent).where(OrderIntent.state == OrderState.AWAITING_HUMAN.value)
            ).scalars()
        )

    def realised_loss_since(self, since: dt.datetime) -> Decimal:
        """Money lost on intents that ended since ``since``, as a positive number.

        Counts unwinds and fees, because a strategy profitable except for the
        cost of getting out of failed baskets is not profitable. Profitable
        intents contribute nothing rather than offsetting: a daily *loss* limit
        that netted against wins would let a bad afternoon hide behind a good
        morning and keep trading through both.
        """
        rows = self._session.execute(
            select(OrderIntent).where(OrderIntent.updated_ts >= since)
        ).scalars()
        loss = ZERO
        for row in rows:
            net = Decimal(row.recovered) - Decimal(row.spent)
            if net < ZERO:
                loss += -net
        return loss
