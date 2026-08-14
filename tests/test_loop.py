"""Detection to a basket waiting for a person.

The property under test throughout is that the loop's happy path ends at
"waiting" and never at "ordered". A loop that could arm itself would make
FR-016's third gate decorative -- present in the design, never actually waited
on by anything.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from arbbot.config import RiskLimits
from arbbot.execution.loop import Candidate, TradingLoop
from arbbot.execution.store import ExecutionStore
from arbbot.reasons import RejectionReason
from arbbot.risk import HaltCause, RiskGate, TradingHalt
from arbbot.states import OrderState, is_terminal

D = Decimal
T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)


def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional_usd=D("100"),
        max_unmatched_exposure_usd=D("150"),
        max_total_open_exposure_usd=D("300"),
        daily_loss_limit_usd=D("50"),
        min_net_edge_usd=D("0.05"),
        max_quote_age_ms=2000,
    )


def candidate(intent_id: str = "c1", *, approved: bool = True, net_edge: str = "1.00") -> Candidate:
    return Candidate(
        intent_id=intent_id,
        relationship_slug="kalshi:TEST",
        relationship_approved=approved,
        legs=(("A", D("0.30")), ("B", D("0.30")), ("C", D("0.30"))),
        quantity=D("10"),
        net_edge=D(net_edge),
        quote_age=dt.timedelta(milliseconds=100),
    )


def loop(session: Session, halt: TradingHalt | None = None) -> TradingLoop:
    return TradingLoop(ExecutionStore(session), RiskGate(limits()), halt or TradingHalt())


class TestNothingIsOrdered:
    def test_a_clean_candidate_ends_up_waiting(self, session: Session) -> None:
        store = ExecutionStore(session)
        report = loop(session).cycle([candidate()], now=T0)

        assert report.awaiting == 1
        row = store.find("c1")
        assert row is not None
        assert row.state == OrderState.AWAITING_HUMAN.value

    def test_the_report_says_nothing_was_ordered(self, session: Session) -> None:
        rendered = loop(session).cycle([candidate()], now=T0).render()
        assert "Nothing has been ordered" in rendered

    def test_no_leg_orders_are_written(self, session: Session) -> None:
        """The loop never reaches a gateway, so nothing can have been sent."""
        from arbbot.db.models import LegOrder

        loop(session).cycle([candidate()], now=T0)
        assert session.query(LegOrder).count() == 0


class TestHalt:
    def test_a_halt_stops_the_loop_before_pricing(self, session: Session) -> None:
        """Pricing candidates that cannot be traded produces a record of
        opportunities that were never real -- a table somebody later counts."""
        halt = TradingHalt()
        halt.trip(HaltCause.DAILY_LOSS, "hit the limit", now=T0)

        report = loop(session, halt).cycle([candidate()], now=T0)

        assert report.halted is True
        assert report.considered == 0
        assert ExecutionStore(session).find("c1") is None

    def test_the_halt_reason_is_reported(self, session: Session) -> None:
        halt = TradingHalt()
        halt.trip(HaltCause.RECONCILIATION, "an intent could not be resolved", now=T0)
        assert "could not be resolved" in loop(session, halt).cycle([], now=T0).render()


class TestControls:
    def test_an_unapproved_relationship_never_becomes_an_intent(self, session: Session) -> None:
        report = loop(session).cycle([candidate(approved=False)], now=T0)

        assert report.rejected == 1
        assert ExecutionStore(session).find("c1") is None

    def test_a_risk_refusal_is_recorded_rather_than_discarded(self, session: Session) -> None:
        """A refused candidate is evidence. Dropping it would leave the archive
        unable to say why nothing traded."""
        store = ExecutionStore(session)
        report = loop(session).cycle([candidate(net_edge="0.01")], now=T0)

        assert report.rejected == 1
        row = store.find("c1")
        assert row is not None
        assert row.state == OrderState.RISK_REJECTED.value
        assert row.reason == str(RejectionReason.NONPOSITIVE_NET_EDGE)

    def test_exposure_from_earlier_candidates_constrains_later_ones(self, session: Session) -> None:
        """The loop reads exposure from the store between candidates, so a
        cycle cannot approve a hundred baskets that each fit on their own."""
        report = loop(session).cycle([candidate(f"c{i}") for i in range(60)], now=T0)

        assert report.awaiting < 60
        assert report.rejected > 0


class TestExpiry:
    def test_an_unanswered_approval_expires(self, session: Session) -> None:
        """A basket approved ten minutes after pricing was priced on quotes
        that are gone."""
        store = ExecutionStore(session)
        loop(session).cycle([candidate()], now=T0)

        later = T0 + dt.timedelta(minutes=5)
        expired = loop(session).expire_stale(now=later)

        assert expired == 1
        row = store.find("c1")
        assert row is not None
        assert row.state == OrderState.EXPIRED.value
        assert row.reason == str(RejectionReason.APPROVAL_EXPIRED)

    def test_a_fresh_approval_is_left_alone(self, session: Session) -> None:
        loop(session).cycle([candidate()], now=T0)
        assert loop(session).expire_stale(now=T0 + dt.timedelta(seconds=5)) == 0

    def test_an_expired_intent_is_terminal(self, session: Session) -> None:
        """It cannot be revived. An approval is bound to what was shown at the
        time, so the loop proposes a new one at current prices instead."""
        loop(session).cycle([candidate()], now=T0)
        loop(session).expire_stale(now=T0 + dt.timedelta(minutes=5))

        assert is_terminal(OrderState.EXPIRED)

    def test_expiry_runs_before_new_candidates_are_considered(self, session: Session) -> None:
        """So a person is never shown a queue in which some entries are already
        meaningless."""
        loop(session).cycle([candidate("old")], now=T0)
        report = loop(session).cycle([candidate("new")], now=T0 + dt.timedelta(minutes=5))

        assert report.expired == 1
        assert report.awaiting == 1
