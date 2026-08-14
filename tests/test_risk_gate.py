"""The controls between a qualified candidate and an order.

These limits sat in configuration from Milestone 0, printed by ``doctor`` and
enforced by nothing. Every test here is a way that could have stayed true in
spirit -- a control present but evaluated against the wrong number, or in the
wrong order, or defaulting open.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from arbbot.config import RiskLimits
from arbbot.reasons import RejectionReason
from arbbot.risk import ExposureSnapshot, OpenIntent, RiskDecision, RiskGate
from arbbot.states import OrderState

D = Decimal
FRESH = dt.timedelta(milliseconds=100)


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional_usd=D("100"),
        max_unmatched_exposure_usd=D("150"),
        max_total_open_exposure_usd=D("300"),
        daily_loss_limit_usd=D("50"),
        min_net_edge_usd=D("0.05"),
        max_quote_age_ms=2000,
    )


@pytest.fixture
def gate(limits: RiskLimits) -> RiskGate:
    return RiskGate(limits)


def decide(
    gate: RiskGate,
    *,
    notional: Decimal = Decimal("50"),
    net_edge: Decimal = Decimal("1.00"),
    quote_age: dt.timedelta | None = FRESH,
    exposure: ExposureSnapshot | None = None,
) -> RiskDecision:
    return gate.evaluate(
        notional=notional,
        net_edge=net_edge,
        quote_age=quote_age,
        exposure=exposure if exposure is not None else ExposureSnapshot(),
    )


def open_intent(
    committed: str, *, state: OrderState = OrderState.FILLED, unmatched: str = "0"
) -> OpenIntent:
    return OpenIntent(intent_id="i1", state=state, committed=D(committed), unmatched=D(unmatched))


class TestHappyPath:
    def test_a_clean_candidate_is_allowed(self, gate: RiskGate) -> None:
        assert decide(gate).allowed is True


class TestHalting:
    def test_an_unknown_intent_blocks_everything(self, gate: RiskGate) -> None:
        """The system does not know what it holds. Placing another order is how
        one unreconciled position becomes several."""
        exposure = ExposureSnapshot(intents=[open_intent("10", state=OrderState.UNKNOWN)])
        decision = decide(gate, exposure=exposure)

        assert decision.allowed is False
        assert decision.reason is RejectionReason.ORDER_STATE_UNKNOWN

    def test_an_incident_blocks_everything(self, gate: RiskGate) -> None:
        exposure = ExposureSnapshot(intents=[open_intent("10", state=OrderState.INCIDENT)])
        assert decide(gate, exposure=exposure).allowed is False

    def test_halting_is_checked_before_anything_else(self, gate: RiskGate) -> None:
        """A candidate that would also fail on size must still report the
        halting state, because that is the condition a human has to clear."""
        exposure = ExposureSnapshot(intents=[open_intent("10", state=OrderState.UNKNOWN)])
        decision = decide(gate, exposure=exposure, notional=D("100000"))
        assert decision.reason is RejectionReason.ORDER_STATE_UNKNOWN


class TestStaleness:
    def test_an_unmeasured_quote_age_is_refused(self, gate: RiskGate) -> None:
        """Refusing by default. An evaluation that cannot say how old its
        inputs were has not measured anything."""
        decision = decide(gate, quote_age=None)
        assert decision.allowed is False
        assert decision.reason is RejectionReason.STALE_QUOTE

    def test_an_old_quote_is_refused(self, gate: RiskGate) -> None:
        assert decide(gate, quote_age=dt.timedelta(seconds=3)).allowed is False

    def test_a_quote_inside_the_limit_passes(self, gate: RiskGate) -> None:
        assert decide(gate, quote_age=dt.timedelta(milliseconds=1999)).allowed is True


class TestEdgeFloor:
    def test_an_edge_below_the_floor_is_refused(self, gate: RiskGate) -> None:
        decision = decide(gate, net_edge=D("0.01"))
        assert decision.allowed is False
        assert decision.reason is RejectionReason.NONPOSITIVE_NET_EDGE

    def test_an_edge_exactly_at_the_floor_passes(self, gate: RiskGate) -> None:
        assert decide(gate, net_edge=D("0.05")).allowed is True


class TestSizeLimits:
    def test_an_oversized_order_is_refused(self, gate: RiskGate) -> None:
        assert decide(gate, notional=D("101")).allowed is False

    def test_a_zero_notional_is_refused(self, gate: RiskGate) -> None:
        assert decide(gate, notional=D("0")).allowed is False

    def test_total_exposure_counts_this_basket_too(self, gate: RiskGate) -> None:
        """The off-by-one worth guarding: checking headroom *before* adding the
        candidate lets the last order breach the cap it respects.

        250 open plus 100 more is 350, over the 300 cap -- even though 250 is
        under it and 100 is a legal order on its own.
        """
        exposure = ExposureSnapshot(intents=[open_intent("250")])
        assert decide(gate, exposure=exposure, notional=D("100")).allowed is False

    def test_total_exposure_allows_what_fits(self, gate: RiskGate) -> None:
        exposure = ExposureSnapshot(intents=[open_intent("250")])
        assert decide(gate, exposure=exposure, notional=D("50")).allowed is True

    def test_a_closed_intent_does_not_count_against_exposure(self, gate: RiskGate) -> None:
        """Terminal intents hold nothing. Counting them would shrink capacity
        permanently over a run."""
        exposure = ExposureSnapshot(intents=[open_intent("250", state=OrderState.SETTLED)])
        assert decide(gate, exposure=exposure, notional=D("100")).allowed is True

    def test_an_unknown_intent_counts_as_exposure(self, gate: RiskGate) -> None:
        """An uncertain venue response is a position until reconciliation says
        otherwise. This is checked through the halting rule, so the assertion
        is that it is not silently treated as free capital."""
        exposure = ExposureSnapshot(intents=[open_intent("250", state=OrderState.UNKNOWN)])
        assert exposure.total_committed == D("250")


class TestUnmatchedExposure:
    def test_the_whole_basket_counts_as_unmatched_while_acquiring(self, gate: RiskGate) -> None:
        """Between the first leg and the last, the entire basket is directional.
        Sizing this control against the residual would understate the window
        that actually carries the risk."""
        exposure = ExposureSnapshot(
            intents=[open_intent("60", state=OrderState.PARTIAL, unmatched="60")]
        )
        assert decide(gate, exposure=exposure, notional=D("100")).allowed is False

    def test_unmatched_within_the_cap_passes(self, gate: RiskGate) -> None:
        exposure = ExposureSnapshot(
            intents=[open_intent("40", state=OrderState.PARTIAL, unmatched="40")]
        )
        assert decide(gate, exposure=exposure, notional=D("100")).allowed is True


class TestDailyLoss:
    def test_reaching_the_daily_loss_limit_stops_trading(self, gate: RiskGate) -> None:
        exposure = ExposureSnapshot(realised_loss_today=D("50"))
        decision = decide(gate, exposure=exposure)

        assert decision.allowed is False
        assert "no further orders today" in decision.detail

    def test_below_the_limit_still_trades(self, gate: RiskGate) -> None:
        assert decide(gate, exposure=ExposureSnapshot(realised_loss_today=D("49"))).allowed is True
