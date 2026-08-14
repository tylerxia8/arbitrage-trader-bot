"""Stopping, and staying stopped.

The risk gate refuses candidates one at a time, which is not a kill switch: a
system at its daily loss limit re-asks every cycle and resumes the moment a
rounding movement puts it a cent back under. These tests are about the
difference between refusing and latching.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from arbbot.risk.halt import HaltCause, TradingHalt

D = Decimal
T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)


class TestLatching:
    def test_a_fresh_halt_permits_trading(self) -> None:
        assert TradingHalt().state.may_trade is True

    def test_tripping_stops_trading(self) -> None:
        halt = TradingHalt()
        halt.trip(HaltCause.MANUAL, "operator pulled the switch", now=T0)

        assert halt.state.may_trade is False
        assert halt.state.cause is HaltCause.MANUAL

    def test_a_halt_does_not_clear_on_its_own(self) -> None:
        """The whole point. Re-checking a condition that has improved is not
        the same as somebody deciding it is safe to resume."""
        halt = TradingHalt()
        halt.check_daily_loss(D("50"), D("50"), now=T0)
        assert halt.state.may_trade is False

        halt.check_daily_loss(D("0"), D("50"), now=T0 + dt.timedelta(hours=1))
        assert halt.state.may_trade is False, "the loss went away; the halt did not"

    def test_the_first_cause_survives_a_second_trip(self) -> None:
        """A daily-loss halt re-tripped by a feed outage is still principally a
        daily-loss halt, and overwriting it would erase the thing that actually
        needs investigating."""
        halt = TradingHalt()
        halt.trip(HaltCause.DAILY_LOSS, "hit the limit", now=T0)
        halt.trip(HaltCause.FEED_OUTAGE, "feed died too", now=T0 + dt.timedelta(minutes=1))

        assert halt.state.cause is HaltCause.DAILY_LOSS
        assert halt.state.since == T0


class TestClearing:
    def test_clearing_needs_a_named_person(self) -> None:
        """A halt cleared by "the system" is a halt nobody examined."""
        halt = TradingHalt()
        halt.trip(HaltCause.MANUAL, "stopped", now=T0)

        with pytest.raises(ValueError, match="named person"):
            halt.clear(operator="", reason="looks fine now")

    def test_clearing_needs_what_was_established(self) -> None:
        halt = TradingHalt()
        halt.trip(HaltCause.MANUAL, "stopped", now=T0)

        with pytest.raises(ValueError, match="what was established"):
            halt.clear(operator="tyler", reason="")

    def test_a_cleared_halt_permits_trading(self) -> None:
        halt = TradingHalt()
        halt.trip(HaltCause.DAILY_LOSS, "hit the limit", now=T0)
        halt.clear(operator="tyler", reason="reviewed the day's fills; the limit was correct")

        assert halt.state.may_trade is True
        assert halt.state.cause is None

    def test_the_trip_is_kept_in_history(self) -> None:
        halt = TradingHalt()
        halt.trip(HaltCause.DAILY_LOSS, "hit the limit", now=T0)
        halt.clear(operator="tyler", reason="reviewed")

        assert len(halt.history) == 1
        assert halt.history[0].cause is HaltCause.DAILY_LOSS


class TestRefusals:
    def test_a_halt_without_a_reason_is_impossible(self) -> None:
        """The person clearing it later has only that sentence to work from."""
        with pytest.raises(ValueError, match="must say what happened"):
            TradingHalt().trip(HaltCause.MANUAL, "   ")


class TestAutomaticTrips:
    def test_the_daily_loss_limit_latches(self) -> None:
        halt = TradingHalt()
        halt.check_daily_loss(D("50"), D("50"), now=T0)
        assert halt.state.cause is HaltCause.DAILY_LOSS

    def test_below_the_limit_does_not_trip(self) -> None:
        halt = TradingHalt()
        halt.check_daily_loss(D("49.99"), D("50"), now=T0)
        assert halt.state.may_trade is True

    def test_an_unresolved_intent_latches(self) -> None:
        """Reconciliation returning INCIDENT means the system does not know
        what it holds, and no amount of further trading improves that."""
        halt = TradingHalt()
        halt.check_unresolved(1, now=T0)

        assert halt.state.cause is HaltCause.RECONCILIATION

    def test_no_unresolved_intents_does_not_trip(self) -> None:
        assert TradingHalt().check_unresolved(0, now=T0).may_trade is True

    def test_a_dead_feed_latches(self) -> None:
        """The freshness gate would reject those candidates anyway, but one at
        a time and silently -- looking exactly like a quiet market rather than
        a broken feed."""
        halt = TradingHalt()
        halt.check_feed(dt.timedelta(minutes=5), dt.timedelta(minutes=2), now=T0)

        assert halt.state.cause is HaltCause.FEED_OUTAGE

    def test_a_live_feed_does_not_trip(self) -> None:
        halt = TradingHalt()
        halt.check_feed(dt.timedelta(seconds=30), dt.timedelta(minutes=2), now=T0)
        assert halt.state.may_trade is True


class TestReporting:
    def test_a_halted_state_says_it_will_not_clear_itself(self) -> None:
        halt = TradingHalt()
        halt.trip(HaltCause.DAILY_LOSS, "hit the limit", now=T0)
        rendered = halt.state.render()

        assert "HALTED" in rendered
        assert "named person" in rendered
        assert "will not clear on its own" in rendered

    def test_a_permitted_state_says_so_plainly(self) -> None:
        assert "permitted" in TradingHalt().state.render()
