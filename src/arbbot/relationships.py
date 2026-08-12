"""Domain vocabulary for the relationship registry.

A "relationship" is a human-approved claim that a set of contracts stands in a
known logical arrangement -- that three outcomes are mutually exclusive and
collectively exhaustive, or that A implies B. Every arbitrage this system can
detect is downstream of one of these claims being true.

Which is why the registry is not a cache of things the system inferred. It is
a record of things a person read the settlement terms and signed for. The
status values below encode that: nothing qualifies from ``PENDING``, and any
change to the underlying terms drops an approved relationship back to
``SUSPENDED`` automatically (FR-004).
"""

from __future__ import annotations

import enum

__all__ = ["ApprovalDecision", "RelationshipStatus", "RelationshipType"]


class RelationshipType(enum.StrEnum):
    """The logical arrangements this system knows how to price."""

    EXHAUSTIVE_BASKET = "exhaustive_basket"
    """Mutually exclusive, collectively exhaustive outcomes; YES on all legs."""

    IMPLICATION_PAIR = "implication_pair"
    """A implies B; NO on A plus YES on B."""

    INTERVAL_PARTITION = "interval_partition"
    """Non-overlapping intervals covering every possible result."""


class RelationshipStatus(enum.StrEnum):
    """Lifecycle of a registry entry."""

    PENDING = "pending"
    """Drafted but unapproved. Can never qualify a candidate (FR-005)."""

    APPROVED = "approved"
    """A human signed for it and the dependency hashes still match."""

    SUSPENDED = "suspended"
    """Material terms changed. Requires re-approval before use (FR-004)."""

    RETIRED = "retired"
    """Withdrawn. Kept for audit; never evaluated."""

    @property
    def may_qualify(self) -> bool:
        """Whether a candidate built on this relationship may be accepted."""
        return self is RelationshipStatus.APPROVED


class ApprovalDecision(enum.StrEnum):
    """A reviewer's recorded verdict on a specific relationship version."""

    APPROVED = "approved"
    REJECTED = "rejected"
