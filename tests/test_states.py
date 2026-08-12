"""Order intent state machine."""

from __future__ import annotations

import pytest

from arbbot.states import (
    IllegalTransitionError,
    OrderState,
    assert_transition,
    can_transition,
    halts_strategy,
    is_exposed,
    is_terminal,
)

TERMINAL = {
    OrderState.RISK_REJECTED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
    OrderState.FAILED,
    OrderState.INCIDENT,
    OrderState.SETTLED,
}


class TestTransitionTable:
    def test_documented_transitions_are_legal(self) -> None:
        assert can_transition(OrderState.PROPOSED, OrderState.RISK_APPROVED)
        assert can_transition(OrderState.RISK_APPROVED, OrderState.AWAITING_HUMAN)
        assert can_transition(OrderState.AWAITING_HUMAN, OrderState.SUBMITTING)
        assert can_transition(OrderState.SUBMITTING, OrderState.PARTIAL)
        assert can_transition(OrderState.PARTIAL, OrderState.HEDGING)
        assert can_transition(OrderState.FILLED, OrderState.SETTLED)

    def test_skipping_risk_approval_is_impossible(self) -> None:
        """The detector may propose. Only the risk engine may authorise."""
        assert not can_transition(OrderState.PROPOSED, OrderState.SUBMITTING)

    def test_rejected_intents_cannot_resume(self) -> None:
        assert not can_transition(OrderState.RISK_REJECTED, OrderState.SUBMITTING)
        assert not can_transition(OrderState.REJECTED, OrderState.SUBMITTING)
        assert not can_transition(OrderState.EXPIRED, OrderState.SUBMITTING)

    def test_assert_transition_names_the_legal_options(self) -> None:
        with pytest.raises(IllegalTransitionError) as exc:
            assert_transition(OrderState.PROPOSED, OrderState.SETTLED)
        assert "risk_approved" in str(exc.value)

    def test_every_state_is_reachable_from_proposed(self) -> None:
        """An unreachable state is dead code in a safety-critical machine."""
        seen = {OrderState.PROPOSED}
        frontier = [OrderState.PROPOSED]
        while frontier:
            current = frontier.pop()
            for state in OrderState:
                if can_transition(current, state) and state not in seen:
                    seen.add(state)
                    frontier.append(state)
        assert seen == set(OrderState)

    def test_terminal_states_match_the_specification(self) -> None:
        assert {s for s in OrderState if is_terminal(s)} == TERMINAL


class TestUnknownState:
    def test_unknown_may_only_reconcile(self) -> None:
        """An uncertain venue response must never be retried directly.

        Assuming a leg did not fill and re-sending it is how one position
        becomes two, which is the opposite of the guaranteed payout the basket
        was constructed to hold.
        """
        targets = {s for s in OrderState if can_transition(OrderState.UNKNOWN, s)}
        assert targets == {OrderState.RECONCILING}

    def test_unknown_counts_as_exposure(self) -> None:
        assert is_exposed(OrderState.UNKNOWN)

    def test_unknown_halts_the_strategy(self) -> None:
        assert halts_strategy(OrderState.UNKNOWN)
        assert halts_strategy(OrderState.INCIDENT)

    def test_every_submission_path_can_reach_unknown(self) -> None:
        """Any state that talks to the venue can lose contact with it."""
        for state in (OrderState.SUBMITTING, OrderState.HEDGING, OrderState.UNWINDING):
            assert can_transition(state, OrderState.UNKNOWN), state


class TestExposure:
    def test_pre_submission_states_are_not_exposed(self) -> None:
        for state in (
            OrderState.PROPOSED,
            OrderState.RISK_APPROVED,
            OrderState.AWAITING_HUMAN,
            OrderState.RISK_REJECTED,
        ):
            assert not is_exposed(state), state

    def test_post_submission_states_are_exposed(self) -> None:
        for state in (OrderState.SUBMITTING, OrderState.PARTIAL, OrderState.FILLED):
            assert is_exposed(state), state
