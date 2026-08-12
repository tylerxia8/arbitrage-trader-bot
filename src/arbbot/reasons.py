"""Rejection reason codes.

Every rejected evaluation and intent is persisted with one of these codes
(FR-018). The catalog is closed on purpose: a free-text rejection reason
cannot be aggregated, and the daily falsification report is only useful if
"why did nothing qualify today" has a countable answer.

Adding a code is a schema change -- update the catalog in the specification
and the daily report grouping at the same time.
"""

from __future__ import annotations

import enum

__all__ = ["RejectionReason"]


class RejectionReason(enum.StrEnum):
    """Closed catalog of reasons a candidate or intent was refused."""

    RELATIONSHIP_NOT_APPROVED = "relationship_not_approved"
    """Rule lacks current human approval."""

    TERMS_CHANGED = "terms_changed"
    """Material terms hash no longer matches the approval dependency."""

    MARKET_NOT_OPEN = "market_not_open"
    """One or more legs unavailable."""

    BOOK_INCOMPLETE = "book_incomplete"
    """Sequence gap or incomplete snapshot."""

    STALE_QUOTE = "stale_quote"
    """Book exceeds the configured maximum age."""

    INSUFFICIENT_DEPTH = "insufficient_depth"
    """Required quantity unavailable at the evaluated prices."""

    UNKNOWN_FEE = "unknown_fee"
    """No effective verified fee rule. Never treated as zero."""

    NONPOSITIVE_NET_EDGE = "nonpositive_net_edge"
    """Full-cost edge does not exceed the configured threshold."""

    RISK_LIMIT = "risk_limit"
    """Capital, exposure, loss, or strategy limit failed."""

    APPROVAL_EXPIRED = "approval_expired"
    """Manual live approval is no longer valid."""

    DUPLICATE_INTENT = "duplicate_intent"
    """Idempotency protection caught a repeat."""

    ORDER_STATE_UNKNOWN = "order_state_unknown"
    """Venue and local order state disagree or are uncertain."""

    RECONCILIATION_DIFFERENCE = "reconciliation_difference"
    """Venue and shadow ledger disagree."""

    @property
    def halts_trading(self) -> bool:
        """Whether observing this reason should stop activity, not just skip a candidate.

        Most reasons are ordinary: a stale book or a thin book means "not this
        one, not right now". These three mean the system's model of the world
        is wrong, and continuing to trade on a wrong model is how a bounded
        loss becomes an unbounded one.
        """
        return self in {
            RejectionReason.ORDER_STATE_UNKNOWN,
            RejectionReason.RECONCILIATION_DIFFERENCE,
            RejectionReason.TERMS_CHANGED,
        }
