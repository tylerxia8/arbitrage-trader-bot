"""Account vocabulary for the shadow ledger (FR-013, EPIC-12).

Double entry, because the alternative is a running balance that drifts. A
single mutable number cannot be audited: when it disagrees with the venue
there is nothing to reconstruct it from, and reconciliation becomes a matter
of opinion. Entries that must sum to zero can be replayed, checked, and
diffed against reality -- which is what the reconciler in Milestone 4 exists
to do.

The sign convention is fixed once here and never negotiated: a **debit is
positive**, a **credit is negative**, and every transaction's entries sum to
exactly zero. Getting that backwards somewhere would let a purchase increase
cash, and the resulting ledger would balance perfectly while describing a
system that prints money.
"""

from __future__ import annotations

import enum

__all__ = ["POSITION_PREFIX", "AccountKind", "position_account"]

#: Positions are per-contract accounts, named ``position:<ticker>``.
POSITION_PREFIX = "position:"


class AccountKind(enum.StrEnum):
    """The fixed accounts. Positions are dynamic and prefixed separately."""

    CASH = "cash"
    """Unencumbered funds. Never allowed to go negative -- a shadow ledger
    that permits an overdraft is simulating a facility nobody has."""

    OPENING_CAPITAL = "opening_capital"
    """Money put in by the owner. The contra-account for a deposit.

    Exists so that funding the account is an ordinary balanced transaction
    rather than a special case. Without it, a deposit either unbalances the
    ledger or has to be booked against profit -- and capital paid in is not
    profit, however convenient it would be for the P&L line.
    """

    RESERVED = "reserved"
    """Funds earmarked for an intent that has not filled yet.

    Reserving before ordering is what stops two candidates spending the same
    dollar. Multi-leg baskets are acquired sequentially, so the window between
    committing to a basket and finishing it is precisely when a second
    candidate would otherwise be evaluated against cash already spoken for.
    """

    FEES = "fees"
    """Cumulative venue fees. Separate from acquisition cost so the
    profitability review can attribute what fees actually consumed."""

    REALIZED_PNL = "realized_pnl"
    """Settled outcomes. The only account that answers "did this make money"."""

    UNWIND_LOSS = "unwind_loss"
    """Cost of closing a basket that could not be completed.

    Kept apart from realized P&L because a strategy that is profitable except
    for its unwinds is not profitable, and merging them hides exactly the
    failure mode multi-leg execution is exposed to.
    """


def position_account(ticker: str) -> str:
    """Account name holding contracts of ``ticker`` at acquisition cost."""
    if not ticker:
        raise ValueError("a position needs a ticker")
    return f"{POSITION_PREFIX}{ticker}"
