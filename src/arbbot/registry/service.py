"""The relationship registry (FR-004, FR-005, EPIC-6).

A relationship is a claim that a set of contracts stands in a known logical
arrangement -- that six temperature buckets are mutually exclusive and
collectively exhaustive, so holding all of them pays exactly one dollar. Every
arbitrage this system can detect is downstream of such a claim being *true*.

Which is why the registry is not a cache of things the system inferred. It is
a record of things a person read the settlement terms and signed for, and the
whole module is built around making that difference impossible to blur:

**Drafting is not approving.** A relationship enters as ``PENDING`` and cannot
qualify anything. :mod:`arbbot.venues.kalshi.discovery` can propose one from
market structure; that proposal is a request for review, not a finding.

**Approval names a person and what they read.** An approval without a reviewer
and their evidence is not an audit trail, and the record is immutable --
withdrawing means recording a new decision, not editing the old one.

**Approval is bound to the terms it was given for.** Every leg's terms hash is
captured at approval. If any of them changes, the relationship suspends
automatically, because a basket that was exhaustive under the old wording may
not be under the new one. That check runs on every evaluation rather than on a
schedule: the registry never assumes the legs are still what the reviewer saw.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.collection.health import utc_now
from arbbot.db.models import Approval, RelationshipRecord
from arbbot.reasons import RejectionReason
from arbbot.relationships import ApprovalDecision, RelationshipStatus, RelationshipType

__all__ = ["RegistryError", "RelationshipRegistry", "UsabilityCheck"]


class RegistryError(RuntimeError):
    """An operation the registry refuses to perform."""


@dataclass(frozen=True, slots=True)
class UsabilityCheck:
    """Whether a relationship may qualify a candidate right now."""

    usable: bool
    reason: RejectionReason | None = None
    detail: str = ""

    @classmethod
    def ok(cls) -> UsabilityCheck:
        return cls(usable=True)

    @classmethod
    def refused(cls, reason: RejectionReason, detail: str) -> UsabilityCheck:
        return cls(usable=False, reason=reason, detail=detail)


class RelationshipRegistry:
    """Drafts, approves, and gates logical relationships."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- drafting --------------------------------------------------------
    def draft(
        self,
        *,
        slug: str,
        relationship_type: RelationshipType,
        legs: list[dict[str, Any]],
        payout_proof: dict[str, Any],
        dependency_hashes: dict[str, str],
        notes: str | None = None,
    ) -> RelationshipRecord:
        """Record a proposed relationship as ``PENDING``.

        Deliberately cheap and deliberately inert. Anything may draft --
        including an AI agent reading market structure -- because a draft
        cannot qualify a candidate. The expensive, restricted step is
        :meth:`approve`.
        """
        if len(legs) < 2:
            raise RegistryError("a relationship needs at least two legs")
        if not dependency_hashes:
            raise RegistryError(
                "a draft must record the terms hash of every leg; without them an "
                "approval cannot be bound to the wording it was given for"
            )

        version = self._next_version(slug)
        record = RelationshipRecord(
            slug=slug,
            version=version,
            relationship_type=relationship_type,
            status=RelationshipStatus.PENDING,
            legs=legs,
            payout_proof=payout_proof,
            dependency_hashes=dependency_hashes,
            notes=notes,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def _next_version(self, slug: str) -> int:
        latest = self._session.execute(
            select(RelationshipRecord.version)
            .where(RelationshipRecord.slug == slug)
            .order_by(RelationshipRecord.version.desc())
            .limit(1)
        ).scalar()
        return int(latest or 0) + 1

    # -- approval --------------------------------------------------------
    def approve(
        self,
        record: RelationshipRecord,
        *,
        reviewer: str,
        evidence: str,
        scope: dict[str, Any] | None = None,
        at: dt.datetime | None = None,
    ) -> Approval:
        """Record a human's approval of one relationship version.

        :param reviewer: an authenticated human identity. Never a model, never
            a service account -- the point of this record is that a person is
            answerable for the claim.
        :param evidence: what they read. Settlement wording, the source URL,
            the quoted rule. An approval that cannot say what was reviewed is
            a rubber stamp with a name on it.
        """
        if not reviewer.strip():
            raise RegistryError("an approval must name its reviewer")
        if not evidence.strip():
            raise RegistryError(
                "an approval must record the evidence read; the reviewer's job is to "
                "confirm the settlement terms, and an approval that cannot say what "
                "was confirmed does not establish anything"
            )
        if record.status is RelationshipStatus.RETIRED:
            raise RegistryError("a retired relationship cannot be approved; draft a new version")

        approval = Approval(
            relationship_id=record.id,
            reviewer=reviewer,
            decision=ApprovalDecision.APPROVED,
            decided_ts=at or utc_now(),
            evidence=evidence,
            scope=scope or {},
        )
        self._session.add(approval)
        record.status = RelationshipStatus.APPROVED
        self._session.flush()
        return approval

    def reject(
        self,
        record: RelationshipRecord,
        *,
        reviewer: str,
        evidence: str,
        at: dt.datetime | None = None,
    ) -> Approval:
        """Record a reviewer declining a relationship."""
        approval = Approval(
            relationship_id=record.id,
            reviewer=reviewer,
            decision=ApprovalDecision.REJECTED,
            decided_ts=at or utc_now(),
            evidence=evidence,
            scope={},
        )
        self._session.add(approval)
        record.status = RelationshipStatus.RETIRED
        self._session.flush()
        return approval

    def suspend(self, record: RelationshipRecord, *, why: str) -> None:
        """Withdraw a relationship from use pending re-approval (FR-004)."""
        record.status = RelationshipStatus.SUSPENDED
        record.notes = f"{record.notes or ''}\nsuspended: {why}".strip()
        self._session.flush()

    # -- gating ----------------------------------------------------------
    def check_usable(
        self, record: RelationshipRecord, current_terms: dict[str, str]
    ) -> UsabilityCheck:
        """Whether this relationship may qualify a candidate against ``current_terms``.

        ``current_terms`` maps leg ticker to the terms hash *now*. The check is
        run on every evaluation rather than on a timer, because the window
        between a terms change and the next scheduled check is exactly when a
        basket stops being a basket.
        """
        if not record.status.may_qualify:
            return UsabilityCheck.refused(
                RejectionReason.RELATIONSHIP_NOT_APPROVED,
                f"status is {record.status.value}",
            )

        approved_terms: dict[str, str] = dict(record.dependency_hashes)
        for ticker, approved_hash in approved_terms.items():
            current = current_terms.get(ticker)
            if current is None:
                return UsabilityCheck.refused(
                    RejectionReason.MARKET_NOT_OPEN,
                    f"no current terms for leg {ticker}",
                )
            if current != approved_hash:
                return UsabilityCheck.refused(
                    RejectionReason.TERMS_CHANGED,
                    f"leg {ticker} terms changed since approval",
                )

        missing = set(current_terms) - set(approved_terms)
        if missing:
            # A leg the reviewer never saw. The set they signed for is not the
            # set in front of us, and a basket missing an outcome pays nothing
            # when that outcome occurs.
            return UsabilityCheck.refused(
                RejectionReason.TERMS_CHANGED,
                f"legs not covered by the approval: {sorted(missing)}",
            )

        return UsabilityCheck.ok()

    def approved(self) -> list[RelationshipRecord]:
        """Every relationship currently in the approved state."""
        return list(
            self._session.execute(
                select(RelationshipRecord).where(
                    RelationshipRecord.status == RelationshipStatus.APPROVED
                )
            ).scalars()
        )

    def latest(self, slug: str) -> RelationshipRecord | None:
        return self._session.execute(
            select(RelationshipRecord)
            .where(RelationshipRecord.slug == slug)
            .order_by(RelationshipRecord.version.desc())
            .limit(1)
        ).scalar_one_or_none()
