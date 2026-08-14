"""The operator's side of the human gate.

The loop parks baskets and the executor refuses anything unapproved. Between
those two facts there was nothing: no way to see the queue, no way to answer
it. A gate with no interface is not a gate that is hard to pass -- it is one
that has never been tried.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from arbbot import buildflags
from arbbot.config import RiskLimits
from arbbot.execution import Executor, PaperGateway
from arbbot.execution.loop import Candidate, TradingLoop
from arbbot.execution.operator import ApprovalRefused, OperatorConsole
from arbbot.execution.store import ExecutionStore
from arbbot.risk import RiskGate, TradingHalt
from arbbot.states import OrderState

D = Decimal
T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)
LEGS = (("A", D("0.30")), ("B", D("0.30")), ("C", D("0.30")))
FRESH = dt.timedelta(milliseconds=100)


@pytest.fixture(autouse=True)
def _compiled_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buildflags, "DEMO_EXECUTION_COMPILED_IN", True)


def limits(**overrides: object) -> RiskLimits:
    base: dict[str, object] = {
        "max_order_notional_usd": D("100"),
        "max_unmatched_exposure_usd": D("150"),
        "max_total_open_exposure_usd": D("300"),
        "daily_loss_limit_usd": D("50"),
        "min_net_edge_usd": D("0.05"),
        "max_quote_age_ms": 2000,
    }
    base.update(overrides)
    return RiskLimits(**base)  # type: ignore[arg-type]


def park(session: Session, *, gate: RiskGate) -> ExecutionStore:
    """Put one basket into AWAITING_HUMAN, the way the loop does."""
    store = ExecutionStore(session)
    TradingLoop(store, gate, TradingHalt()).cycle(
        [
            Candidate(
                intent_id="i1",
                relationship_slug="kalshi:TEST",
                relationship_approved=True,
                legs=LEGS,
                quantity=D("10"),
                net_edge=D("1.00"),
                quote_age=FRESH,
            )
        ],
        now=T0,
    )
    return store


def console(store: ExecutionStore, gate: RiskGate, gateway: PaperGateway) -> OperatorConsole:
    return OperatorConsole(store, Executor(gateway, gate, journal=store.journal()))


class TestQueue:
    def test_the_queue_shows_what_is_waiting(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)

        pending = console(store, gate, PaperGateway()).pending(now=T0)
        assert len(pending) == 1
        assert pending[0].intent_id == "i1"
        assert pending[0].notional == D("9.00")

    def test_each_row_says_how_long_it_has_left(self, session: Session) -> None:
        """A basket priced thirty seconds ago is not the basket in front of
        you now."""
        gate = RiskGate(limits())
        store = park(session, gate=gate)

        pending = console(store, gate, PaperGateway()).pending(now=T0 + dt.timedelta(seconds=20))
        assert 9 <= pending[0].expires_in.total_seconds() <= 11
        assert pending[0].lapsed is False

    def test_a_lapsed_row_is_marked(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)

        rendered = console(store, gate, PaperGateway()).render_queue(
            now=T0 + dt.timedelta(minutes=2)
        )
        assert "LAPSED" in rendered

    def test_an_empty_queue_says_so(self, session: Session) -> None:
        gate = RiskGate(limits())
        assert "nothing is waiting" in console(
            ExecutionStore(session), gate, PaperGateway()
        ).render_queue(now=T0)


class TestApprovalRequirements:
    async def test_an_unnamed_reviewer_is_refused(self, session: Session) -> None:
        """ "Approved by the system" records nothing."""
        gate = RiskGate(limits())
        store = park(session, gate=gate)
        gateway = PaperGateway()

        with pytest.raises(ApprovalRefused, match="must name a person"):
            await console(store, gate, gateway).approve(
                "i1", reviewer="  ", evidence="checked", legs=LEGS, quote_age=FRESH, now=T0
            )
        assert gateway.placed == []

    async def test_an_approval_without_evidence_is_refused(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)
        gateway = PaperGateway()

        with pytest.raises(ApprovalRefused, match="what was checked"):
            await console(store, gate, gateway).approve(
                "i1", reviewer="tyler", evidence="", legs=LEGS, quote_age=FRESH, now=T0
            )
        assert gateway.placed == []

    async def test_an_unknown_intent_is_refused(self, session: Session) -> None:
        gate = RiskGate(limits())
        with pytest.raises(ApprovalRefused, match="no intent"):
            await console(ExecutionStore(session), gate, PaperGateway()).approve(
                "nope", reviewer="tyler", evidence="checked", legs=LEGS, quote_age=FRESH, now=T0
            )

    async def test_only_a_parked_basket_can_be_approved(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)
        store.transition("i1", OrderState.REJECTED, now=T0)

        with pytest.raises(ApprovalRefused, match="not awaiting approval"):
            await console(store, gate, PaperGateway()).approve(
                "i1", reviewer="tyler", evidence="checked", legs=LEGS, quote_age=FRESH, now=T0
            )


class TestExpiry:
    async def test_a_stale_approval_is_refused_and_retired(self, session: Session) -> None:
        """Saying yes to a price from two minutes ago is saying yes to a price
        that is gone -- and the freshness gate would reject it a moment later,
        after a person had already committed."""
        gate = RiskGate(limits())
        store = park(session, gate=gate)
        gateway = PaperGateway()

        with pytest.raises(ApprovalRefused, match="expired"):
            await console(store, gate, gateway).approve(
                "i1",
                reviewer="tyler",
                evidence="checked",
                legs=LEGS,
                quote_age=FRESH,
                now=T0 + dt.timedelta(minutes=2),
            )

        row = store.find("i1")
        assert row is not None
        assert row.state == OrderState.EXPIRED.value
        assert gateway.placed == []


class TestApproval:
    async def test_approving_acquires_the_basket(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)
        gateway = PaperGateway()

        result = await console(store, gate, gateway).approve(
            "i1", reviewer="tyler", evidence="checked the book", legs=LEGS, quote_age=FRESH, now=T0
        )

        assert result.state is OrderState.FILLED
        assert len(gateway.placed) == 3
        row = store.find("i1")
        assert row is not None
        assert row.state == OrderState.FILLED.value

    async def test_the_reviewer_and_evidence_are_recorded(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)

        await console(store, gate, PaperGateway()).approve(
            "i1", reviewer="tyler", evidence="checked the book", legs=LEGS, quote_age=FRESH, now=T0
        )
        row = store.find("i1")
        assert row is not None
        assert row.detail is not None

    async def test_a_control_that_now_refuses_expires_the_basket(self, session: Session) -> None:
        """A basket can stop being viable while it waits. The machine has no
        AWAITING_HUMAN to RISK_REJECTED edge and should not -- what happened is
        that it stopped being viable, which is what EXPIRED means here."""
        gate = RiskGate(limits())
        store = park(session, gate=gate)
        gateway = PaperGateway()

        # Approve with a stale quote: the risk gate refuses at submission.
        strict = OperatorConsole(store, Executor(gateway, gate, journal=store.journal()))
        result = await strict.approve(
            "i1",
            reviewer="tyler",
            evidence="checked",
            legs=LEGS,
            quote_age=dt.timedelta(seconds=30),
            now=T0,
        )

        assert result.state is OrderState.RISK_REJECTED
        assert gateway.placed == []
        row = store.find("i1")
        assert row is not None
        assert row.state == OrderState.EXPIRED.value


class TestRejection:
    def test_declining_needs_a_name(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)

        with pytest.raises(ApprovalRefused, match="name the person"):
            console(store, gate, PaperGateway()).reject("i1", reviewer="", reason="too thin")

    def test_a_declined_basket_is_terminal(self, session: Session) -> None:
        gate = RiskGate(limits())
        store = park(session, gate=gate)

        console(store, gate, PaperGateway()).reject(
            "i1", reviewer="tyler", reason="depth looked thinner than the walk suggested"
        )
        row = store.find("i1")
        assert row is not None
        assert row.state == OrderState.REJECTED.value
