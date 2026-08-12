"""Order intent state machine (specification section 21).

The transition table is data, not scattered ``if`` statements, so that it can
be tested exhaustively and so that an illegal transition is impossible to
express rather than merely discouraged.

Two properties matter more than the rest:

*   :data:`OrderState.UNKNOWN` may only move to ``RECONCILING``. An uncertain
    venue response is treated as an open position, because assuming an order
    did not fill and retrying it is how one leg becomes two.
*   Every transition is expected to emit an immutable event, and every retry
    must reuse the same idempotency identity. This module defines the legal
    shape of the machine; the executor (Milestone 4) enforces the event and
    idempotency requirements on top of it.
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = [
    "IllegalTransitionError",
    "OrderState",
    "assert_transition",
    "can_transition",
    "halts_strategy",
    "is_exposed",
    "is_terminal",
]


class OrderState(enum.StrEnum):
    """Lifecycle of a single multi-leg order intent."""

    PROPOSED = "proposed"
    """Detector created the intent. No capital committed."""

    RISK_REJECTED = "risk_rejected"
    """A deterministic control refused it. Terminal."""

    RISK_APPROVED = "risk_approved"
    """All deterministic controls passed."""

    AWAITING_HUMAN = "awaiting_human"
    """Live approval required. Expires quickly."""

    REJECTED = "rejected"
    """A human declined it. Terminal."""

    EXPIRED = "expired"
    """The approval window closed before submission. Terminal."""

    SUBMITTING = "submitting"
    """Leg orders are being sent. Exposure begins here."""

    PARTIAL = "partial"
    """Some but not all intended quantity filled. Directionally exposed."""

    HEDGING = "hedging"
    """Acquiring the missing legs to restore the guaranteed payout."""

    UNWINDING = "unwinding"
    """Closing acquired legs because the basket cannot be completed."""

    UNKNOWN = "unknown"
    """Venue response or state uncertain. Treated as exposure; strategy paused."""

    RECONCILING = "reconciling"
    """Comparing venue and internal state to resolve the truth."""

    FILLED = "filled"
    """The intended basket is held in full."""

    FAILED = "failed"
    """The intent ended without the intended basket. Terminal."""

    INCIDENT = "incident"
    """Reconciliation could not resolve the difference. Terminal; needs a human."""

    SETTLED = "settled"
    """Venue settlement posted and reconciled. Terminal."""


#: Legal transitions. A state absent from this mapping is terminal.
_TRANSITIONS: Final[dict[OrderState, frozenset[OrderState]]] = {
    OrderState.PROPOSED: frozenset({OrderState.RISK_REJECTED, OrderState.RISK_APPROVED}),
    OrderState.RISK_APPROVED: frozenset({OrderState.AWAITING_HUMAN, OrderState.SUBMITTING}),
    OrderState.AWAITING_HUMAN: frozenset(
        {OrderState.EXPIRED, OrderState.REJECTED, OrderState.SUBMITTING}
    ),
    OrderState.SUBMITTING: frozenset(
        {OrderState.PARTIAL, OrderState.FILLED, OrderState.FAILED, OrderState.UNKNOWN}
    ),
    OrderState.PARTIAL: frozenset(
        {OrderState.HEDGING, OrderState.UNWINDING, OrderState.FILLED, OrderState.FAILED}
    ),
    # Hedging and unwinding are the two documented responses to a partial fill.
    # Both can themselves lose contact with the venue, so both may reach UNKNOWN.
    OrderState.HEDGING: frozenset({OrderState.FILLED, OrderState.FAILED, OrderState.UNKNOWN}),
    OrderState.UNWINDING: frozenset({OrderState.FAILED, OrderState.UNKNOWN}),
    OrderState.UNKNOWN: frozenset({OrderState.RECONCILING}),
    OrderState.RECONCILING: frozenset({OrderState.FILLED, OrderState.FAILED, OrderState.INCIDENT}),
    OrderState.FILLED: frozenset({OrderState.SETTLED, OrderState.RECONCILING}),
}

#: States in which capital or a position may be at risk.
_EXPOSED: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.SUBMITTING,
        OrderState.PARTIAL,
        OrderState.HEDGING,
        OrderState.UNWINDING,
        OrderState.UNKNOWN,
        OrderState.RECONCILING,
        OrderState.FILLED,
    }
)

#: States that must stop further strategy execution until a human resolves them.
_HALTING: Final[frozenset[OrderState]] = frozenset({OrderState.UNKNOWN, OrderState.INCIDENT})


class IllegalTransitionError(RuntimeError):
    """Raised when code attempts a transition the machine does not permit."""

    def __init__(self, source: OrderState, target: OrderState) -> None:
        allowed = sorted(s.value for s in _TRANSITIONS.get(source, frozenset()))
        detail = ", ".join(allowed) if allowed else "<terminal>"
        super().__init__(
            f"illegal order transition {source.value} -> {target.value}; allowed: {detail}"
        )
        self.source = source
        self.target = target


def can_transition(source: OrderState, target: OrderState) -> bool:
    """Return whether ``source -> target`` is a legal transition."""
    return target in _TRANSITIONS.get(source, frozenset())


def assert_transition(source: OrderState, target: OrderState) -> None:
    """Raise :class:`IllegalTransitionError` unless the transition is legal."""
    if not can_transition(source, target):
        raise IllegalTransitionError(source, target)


def is_terminal(state: OrderState) -> bool:
    """Return whether the state has no outbound transitions."""
    return state not in _TRANSITIONS


def is_exposed(state: OrderState) -> bool:
    """Return whether capital or a position may be at risk in this state."""
    return state in _EXPOSED


def halts_strategy(state: OrderState) -> bool:
    """Return whether this state must block further execution for the strategy."""
    return state in _HALTING
