"""Persisting what the executor is about to do, and reading exposure back.

The risk gate sizes candidates against what is open. Before this, "what is
open" was whatever the caller passed in -- a limit-shaped argument rather than
a limit. These tests are about that number having a source, and about the
record surviving the failures that actually leave positions behind.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from arbbot import buildflags
from arbbot.config import RiskLimits
from arbbot.db.models import LegOrder, OrderIntent
from arbbot.execution import BasketIntent, Executor, OrderRequest, PaperGateway, leg_key
from arbbot.execution.gateway import OrderOutcome, OrderResult
from arbbot.execution.store import ExecutionStore
from arbbot.risk import RiskGate
from arbbot.states import IllegalTransitionError, OrderState

D = Decimal
T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _compiled_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buildflags, "DEMO_EXECUTION_COMPILED_IN", True)


@pytest.fixture
def gate() -> RiskGate:
    return RiskGate(
        RiskLimits(
            max_order_notional_usd=D("1000"),
            max_unmatched_exposure_usd=D("1000"),
            max_total_open_exposure_usd=D("2000"),
            daily_loss_limit_usd=D("100"),
            min_net_edge_usd=D("0.01"),
            max_quote_age_ms=2000,
        )
    )


def intent(intent_id: str = "intent-1") -> BasketIntent:
    return BasketIntent(
        intent_id=intent_id,
        legs=(("A", D("0.30")), ("B", D("0.30")), ("C", D("0.30"))),
        quantity=D("10"),
        net_edge=D("1.00"),
        quote_age=dt.timedelta(milliseconds=100),
        relationship_approved=True,
        human_approved=True,
    )


class Journal:
    """Adapts the store to the executor's journal protocol."""

    def __init__(self, store: ExecutionStore) -> None:
        self.store = store

    def opened(self, basket: BasketIntent) -> None:
        self.store.open_intent(basket, relationship_slug="kalshi:TEST")
        self.store.transition(basket.intent_id, OrderState.RISK_APPROVED)
        self.store.transition(basket.intent_id, OrderState.SUBMITTING)

    def leg(self, intent_id: str, request: OrderRequest, result: OrderResult, side: str) -> None:
        self.store.record_leg(intent_id, request, result, side=side)

    def ended(self, result: object) -> None:
        return None


class TestWriteOrdering:
    async def test_the_intent_exists_before_any_order_is_sent(
        self, session: Session, gate: RiskGate
    ) -> None:
        """A process that dies mid-acquisition has left real positions at the
        venue. A record written afterwards is missing for exactly those runs."""
        store = ExecutionStore(session)
        seen: list[int] = []

        class Watcher(Journal):
            def leg(
                self, intent_id: str, request: OrderRequest, result: OrderResult, side: str
            ) -> None:
                # By the time the first leg resolves, the intent must be there.
                seen.append(len(list(session.execute(select(OrderIntent)).scalars())))
                super().leg(intent_id, request, result, side)

        executor = Executor(PaperGateway(), gate, journal=Watcher(store))
        await executor.acquire(intent())

        assert seen
        assert seen[0] == 1

    async def test_every_leg_is_recorded(self, session: Session, gate: RiskGate) -> None:
        store = ExecutionStore(session)
        await Executor(PaperGateway(), gate, journal=Journal(store)).acquire(intent())

        legs = list(session.execute(select(LegOrder)).scalars())
        assert [leg.ticker for leg in legs] == ["A", "B", "C"]
        assert all(leg.outcome == OrderOutcome.FILLED.value for leg in legs)

    async def test_an_unwind_is_recorded_as_a_sale(self, session: Session, gate: RiskGate) -> None:
        """Proceeds are kept apart from spend, so a strategy profitable except
        for the cost of getting out cannot look profitable."""
        store = ExecutionStore(session)
        await Executor(PaperGateway(reject={"C"}), gate, journal=Journal(store)).acquire(intent())

        sides = [leg.side for leg in session.execute(select(LegOrder)).scalars()]
        assert sides.count("buy") == 3
        assert sides.count("sell") == 2

        row = store.find("intent-1")
        assert row is not None
        assert Decimal(row.spent) == D("6.00")
        assert Decimal(row.recovered) == D("6.00")


class TestIdempotencyIsEnforced:
    def test_a_duplicate_key_is_refused_by_the_database(self, session: Session) -> None:
        """Discipline is not a sufficient defence against the most expensive
        bug available. A second insert under the same key must fail loudly."""
        store = ExecutionStore(session)
        store.open_intent(intent(), relationship_slug="kalshi:TEST")

        request = OrderRequest(
            idempotency_key=leg_key("intent-1", "A"),
            ticker="A",
            quantity=D("10"),
            limit_price=D("0.30"),
        )
        result = OrderResult(OrderOutcome.FILLED, filled=D("10"), cost=D("3.00"))
        store.record_leg("intent-1", request, result)

        with pytest.raises(IntegrityError):
            store.record_leg("intent-1", request, result)


class TestTransitions:
    def test_an_illegal_transition_is_refused(self, session: Session) -> None:
        """The state machine is the authority. A persistence layer that would
        write an impossible state makes the machine advisory."""
        store = ExecutionStore(session)
        store.open_intent(intent(), relationship_slug="kalshi:TEST")

        with pytest.raises(IllegalTransitionError):
            store.transition("intent-1", OrderState.FILLED)

    def test_a_missing_intent_raises(self, session: Session) -> None:
        with pytest.raises(KeyError):
            ExecutionStore(session).transition("nope", OrderState.RISK_APPROVED)


class TestExposure:
    def test_exposure_is_derived_from_the_rows(self, session: Session) -> None:
        store = ExecutionStore(session)
        store.open_intent(intent(), relationship_slug="kalshi:TEST")
        store.transition("intent-1", OrderState.RISK_APPROVED)
        store.transition("intent-1", OrderState.SUBMITTING)

        snapshot = store.exposure()
        assert len(snapshot.open_intents) == 1
        assert snapshot.total_committed == D("9.00")

    def test_a_terminal_intent_holds_nothing(self, session: Session) -> None:
        store = ExecutionStore(session)
        store.open_intent(intent(), relationship_slug="kalshi:TEST")
        store.transition("intent-1", OrderState.RISK_REJECTED)

        assert store.exposure().open_intents == []

    def test_an_acquiring_intent_is_fully_unmatched(self, session: Session) -> None:
        """Between the first leg and the last there is no hedge at all."""
        store = ExecutionStore(session)
        store.open_intent(intent(), relationship_slug="kalshi:TEST")
        store.transition("intent-1", OrderState.RISK_APPROVED)
        store.transition("intent-1", OrderState.SUBMITTING)

        assert store.exposure().total_unmatched == D("9.00")

    def test_a_filled_basket_is_not_unmatched(self, session: Session) -> None:
        store = ExecutionStore(session)
        store.open_intent(intent(), relationship_slug="kalshi:TEST")
        store.transition("intent-1", OrderState.RISK_APPROVED)
        store.transition("intent-1", OrderState.SUBMITTING)
        store.transition("intent-1", OrderState.FILLED)

        snapshot = store.exposure()
        assert snapshot.total_committed == D("9.00"), "still capital at risk"
        assert snapshot.total_unmatched == D("0"), "but hedged"

    async def test_the_gate_reads_what_the_store_reports(
        self, session: Session, gate: RiskGate
    ) -> None:
        """The point of the whole table: the limit is enforced against stored
        state rather than against an argument."""
        store = ExecutionStore(session)
        await Executor(PaperGateway(), gate, journal=Journal(store)).acquire(intent("intent-1"))

        exposure = store.exposure()
        assert exposure.total_committed > D("0")

        decision = gate.evaluate(
            notional=D("1995"),
            net_edge=D("1"),
            quote_age=dt.timedelta(milliseconds=10),
            exposure=exposure,
        )
        assert decision.allowed is False, "stored exposure counts against the cap"


class TestRealisedLoss:
    def test_a_losing_intent_contributes_its_loss(self, session: Session) -> None:
        store = ExecutionStore(session)
        row = store.open_intent(intent(), relationship_slug="kalshi:TEST")
        row.spent = D("10")
        row.recovered = D("7")
        session.flush()

        assert store.realised_loss_since(T0 - dt.timedelta(days=1)) == D("3")

    def test_a_winning_intent_does_not_offset(self, session: Session) -> None:
        """A daily *loss* limit that netted against wins would let a bad
        afternoon hide behind a good morning and keep trading through both."""
        store = ExecutionStore(session)
        losing = store.open_intent(intent("losing"), relationship_slug="kalshi:TEST")
        losing.spent = D("10")
        losing.recovered = D("7")
        winning = store.open_intent(intent("winning"), relationship_slug="kalshi:TEST")
        winning.spent = D("10")
        winning.recovered = D("20")
        session.flush()

        assert store.realised_loss_since(T0 - dt.timedelta(days=1)) == D("3")
