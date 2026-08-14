"""Acquiring a basket, and failing safely when it cannot be acquired.

Every test here is inside the window between the first leg and the last, where
the position is directional and the guaranteed payout does not exist yet. That
window is the entire risk of this strategy, and the shadow executor has been
modelling it since M3 -- these are the same failures handled for real.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from arbbot import buildflags
from arbbot.config import RiskLimits
from arbbot.execution import BasketIntent, Executor, OrderRequest, PaperGateway, leg_key
from arbbot.reasons import RejectionReason
from arbbot.risk import ExposureSnapshot, OpenIntent, RiskGate
from arbbot.states import OrderState, is_terminal

D = Decimal
FRESH = dt.timedelta(milliseconds=100)


@pytest.fixture(autouse=True)
def _compiled_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm the build flag for these tests only.

    The shipped build has no execution path compiled in, and that is not a
    detail to test around -- ``TestGates`` asserts the refusal explicitly. Here
    it is lifted so the behaviour underneath is reachable at all.
    """
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


def intent(**kwargs: object) -> BasketIntent:
    params: dict[str, object] = {
        "intent_id": "intent-1",
        "legs": (("A", D("0.30")), ("B", D("0.30")), ("C", D("0.30"))),
        "quantity": D("10"),
        "net_edge": D("1.00"),
        "quote_age": FRESH,
        "relationship_approved": True,
        "human_approved": True,
    }
    params.update(kwargs)
    return BasketIntent(**params)  # type: ignore[arg-type]


class TestGates:
    async def test_a_build_without_an_execution_path_refuses(
        self, gate: RiskGate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FR-016's outermost gate, and the one configuration cannot flip."""
        monkeypatch.setattr(buildflags, "DEMO_EXECUTION_COMPILED_IN", False)
        monkeypatch.setattr(buildflags, "LIVE_EXECUTION_COMPILED_IN", False)
        gateway = PaperGateway()

        result = await Executor(gateway, gate).acquire(intent())

        assert result.state is OrderState.RISK_REJECTED
        assert gateway.placed == [], "nothing may reach the venue"

    async def test_an_unapproved_relationship_refuses(self, gate: RiskGate) -> None:
        gateway = PaperGateway()
        result = await Executor(gateway, gate).acquire(intent(relationship_approved=False))

        assert result.reason is RejectionReason.RELATIONSHIP_NOT_APPROVED
        assert gateway.placed == []

    async def test_missing_human_approval_refuses(self, gate: RiskGate) -> None:
        """Per basket, never per session. An approval covering 'whatever the
        bot does next' is not an approval of anything."""
        gateway = PaperGateway()
        result = await Executor(gateway, gate).acquire(intent(human_approved=False))

        assert result.state is OrderState.RISK_REJECTED
        assert gateway.placed == []

    async def test_a_risk_refusal_stops_submission(self, gate: RiskGate) -> None:
        gateway = PaperGateway()
        exposure = ExposureSnapshot(
            intents=[OpenIntent("other", OrderState.UNKNOWN, committed=D("1"))]
        )
        result = await Executor(gateway, gate).acquire(intent(), exposure=exposure)

        assert result.reason is RejectionReason.ORDER_STATE_UNKNOWN
        assert gateway.placed == []

    async def test_a_refusal_is_terminal(self, gate: RiskGate) -> None:
        """A refused intent must not be walkable back into a submission."""
        result = await Executor(PaperGateway(), gate).acquire(intent(relationship_approved=False))
        assert is_terminal(result.state)


class TestAcquisition:
    async def test_a_complete_basket_fills(self, gate: RiskGate) -> None:
        gateway = PaperGateway()
        result = await Executor(gateway, gate).acquire(intent())

        assert result.state is OrderState.FILLED
        assert len(gateway.placed) == 3
        assert result.spent == D("9.00")

    async def test_every_leg_carries_a_stable_key(self, gate: RiskGate) -> None:
        """A retried submit that fills twice turns one leg of a hedged basket
        into a directional position -- the most expensive bug available."""
        gateway = PaperGateway()
        await Executor(gateway, gate).acquire(intent())

        keys = [r.idempotency_key for r in gateway.placed]
        assert keys == [leg_key("intent-1", t) for t in ("A", "B", "C")]
        assert len(set(keys)) == 3

    async def test_the_same_leg_of_a_different_intent_gets_a_different_key(self) -> None:
        assert leg_key("intent-1", "A") != leg_key("intent-2", "A")

    async def test_a_replayed_key_does_not_fill_twice(self) -> None:
        """The property the key exists for, asserted against the gateway."""
        gateway = PaperGateway()
        request = OrderRequest(
            idempotency_key=leg_key("intent-1", "A"),
            ticker="A",
            quantity=D("10"),
            limit_price=D("0.30"),
        )
        first = await gateway.place(request)
        second = await gateway.place(request)

        assert first == second
        assert len(gateway.placed) == 1


class TestPartialFills:
    async def test_a_partial_basket_is_unwound(self, gate: RiskGate) -> None:
        """A partial basket is not a smaller arbitrage. It is a bet nobody
        chose, and the executor's job is to get out of it."""
        gateway = PaperGateway(reject={"C"})
        result = await Executor(gateway, gate).acquire(intent())

        assert result.state is OrderState.FAILED
        assert set(result.unwound) == {"A", "B"}
        assert [r.ticker for r in gateway.sold] == ["A", "B"]

    async def test_the_third_leg_is_not_attempted_after_a_partial(self, gate: RiskGate) -> None:
        """Buying on after a short fill deepens a position that already cannot
        become the basket that was priced."""
        gateway = PaperGateway(fill_ratio=D("0.5"))
        await Executor(gateway, gate).acquire(intent())

        assert [r.ticker for r in gateway.placed] == ["A"]

    async def test_a_rejection_on_the_first_leg_leaves_nothing_to_unwind(
        self, gate: RiskGate
    ) -> None:
        gateway = PaperGateway(reject={"A"})
        result = await Executor(gateway, gate).acquire(intent())

        assert result.state is OrderState.FAILED
        assert result.acquired == {}
        assert gateway.sold == []

    async def test_the_unwind_loss_is_reported(self, gate: RiskGate) -> None:
        gateway = PaperGateway(reject={"C"})
        result = await Executor(gateway, gate).acquire(intent())

        # The paper gateway sells back at the same limit, so this run is flat.
        # What matters is that spent and recovered are both accounted, and that
        # realised is derived from them rather than assumed to be zero.
        assert result.spent == D("6.00")
        assert result.recovered == D("6.00")
        assert result.realised == D("0")


class TestUncertainty:
    async def test_an_unknown_response_stops_the_whole_intent(self, gate: RiskGate) -> None:
        """Not just the leg. Buying on risks completing a basket on a position
        that may not exist; unwinding risks selling what was never bought."""
        gateway = PaperGateway(vanish={"B"})
        result = await Executor(gateway, gate).acquire(intent())

        assert result.state is OrderState.UNKNOWN
        assert result.needs_human is True
        assert [r.ticker for r in gateway.placed] == ["A", "B"]
        assert gateway.sold == [], "no unwind while the position is uncertain"

    async def test_an_unknown_unwind_is_also_uncertain(self, gate: RiskGate) -> None:
        gateway = PaperGateway(reject={"C"}, vanish={"B"})
        result = await Executor(gateway, gate).acquire(intent())

        assert result.state is OrderState.UNKNOWN
        assert result.needs_human is True

    async def test_an_unknown_key_is_not_cached(self) -> None:
        """Replaying the key must reach the venue again rather than replay a
        guess about what happened."""
        gateway = PaperGateway(vanish={"A"})
        request = OrderRequest(
            idempotency_key=leg_key("i", "A"), ticker="A", quantity=D("1"), limit_price=D("0.3")
        )
        await gateway.place(request)
        await gateway.place(request)

        assert len(gateway.placed) == 2


class TestRequestValidation:
    def test_an_order_without_a_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="idempotency key"):
            OrderRequest(idempotency_key="", ticker="A", quantity=D("1"), limit_price=D("0.3"))

    def test_an_order_for_nothing_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not an order"):
            OrderRequest(idempotency_key="k", ticker="A", quantity=D("0"), limit_price=D("0.3"))

    def test_a_nonpositive_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="limit price"):
            OrderRequest(idempotency_key="k", ticker="A", quantity=D("1"), limit_price=D("0"))
