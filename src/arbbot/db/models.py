"""Core persistence model.

Scope at Milestone 0 is the evidence foundation: the immutable raw archive
that everything replays from, the normalized market and terms records, the
approval registry, and the append-only audit log. Candidate, evaluation,
order, ledger, and reconciliation tables arrive with the milestones that
earn them.

The organising constraint is that a decision must be reproducible from stored
inputs alone (NFR-03). That is why :class:`RawMessage` keeps the untouched
payload and its hash rather than only the parsed result -- when the parser
changes, every past decision must still be re-derivable from what the venue
actually sent, not from what an older parser believed it meant.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from arbbot.db.base import Base, Json, Sha256, Timestamp
from arbbot.relationships import ApprovalDecision, RelationshipStatus, RelationshipType

__all__ = [
    "Approval",
    "AuditEvent",
    "Market",
    "RawMessage",
    "RelationshipRecord",
    "TermsVersion",
]


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class RawMessage(Base):
    """Immutable archive of everything the venue sent.

    Append-only. Nothing in this system may update or delete a row here: the
    replay guarantee and every audit trail bottom out in this table being a
    faithful record of the wire.
    """

    __tablename__ = "raw_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(128))
    """REST endpoint path or WebSocket channel name."""

    sequence: Mapped[int | None] = mapped_column(BigInteger)
    """Venue sequence number where the channel provides one. Gaps are detectable
    only because this is stored verbatim, including when it goes backwards."""

    source_ts: Mapped[dt.datetime | None] = mapped_column()
    """Venue-stamped time, when present. Not trusted for staleness on its own."""

    received_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    """Local receive time. Staleness is measured against this."""

    payload: Mapped[Json]
    sha256: Mapped[Sha256]
    schema_version: Mapped[str] = mapped_column(String(32))
    """Parser contract this payload was captured under. A change here
    invalidates cached interpretations, not the payload itself."""

    __table_args__ = (
        Index("ix_raw_message_channel_received", "channel", "received_ts"),
        Index("ix_raw_message_venue_sequence", "venue", "channel", "sequence"),
        UniqueConstraint("venue", "channel", "sha256", name="uq_raw_message_dedupe"),
    )


class Market(Base):
    """Normalized view of a single tradeable contract."""

    __tablename__ = "market"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    venue: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))

    close_ts: Mapped[dt.datetime | None] = mapped_column()
    settlement_ts: Mapped[dt.datetime | None] = mapped_column()

    terms_hash: Mapped[Sha256 | None] = mapped_column()
    """Hash of the current normalized settlement terms. When this stops
    matching an approval's recorded dependency, the relationship suspends."""

    first_seen_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    last_seen_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())

    terms_versions: Mapped[list[TermsVersion]] = relationship(
        back_populates="market", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("venue", "ticker", name="uq_market_venue_ticker"),
        Index("ix_market_event", "venue", "event_id"),
    )


class TermsVersion(Base):
    """A point-in-time capture of a market's settlement terms.

    Kept as a version series rather than a mutable column because "the terms
    changed" is the single most dangerous event for a logical-arbitrage system:
    a basket that was exhaustive under the old wording may not be under the new
    one, and detecting that requires both versions side by side.
    """

    __tablename__ = "terms_version"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("market.id", ondelete="CASCADE"))
    fetched_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())

    raw_terms: Mapped[str] = mapped_column(Text)
    normalized_terms: Mapped[Json]
    terms_hash: Mapped[Sha256]
    parser_version: Mapped[str] = mapped_column(String(32))

    market: Mapped[Market] = relationship(back_populates="terms_versions")

    __table_args__ = (
        UniqueConstraint("market_id", "terms_hash", name="uq_terms_version_market_hash"),
        Index("ix_terms_version_market_fetched", "market_id", "fetched_ts"),
    )


class RelationshipRecord(Base):
    """A versioned logical claim about a set of contracts.

    ``dependency_hashes`` records the exact terms hash of every leg at approval
    time. The registry does not trust that the legs are still what the reviewer
    read -- it re-checks, every evaluation.
    """

    __tablename__ = "relationship"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(128))
    """Stable human-readable identity, shared across versions."""

    version: Mapped[int] = mapped_column(Integer)
    relationship_type: Mapped[RelationshipType] = mapped_column(
        Enum(RelationshipType, name="relationship_type", native_enum=False)
    )
    status: Mapped[RelationshipStatus] = mapped_column(
        Enum(RelationshipStatus, name="relationship_status", native_enum=False),
        default=RelationshipStatus.PENDING,
    )

    legs: Mapped[Json]
    """Leg definitions: venue, ticker, side, and quantity ratio per leg."""

    payout_proof: Mapped[Json]
    """Truth table or formal implication proof establishing the minimum payout
    across every enumerated state. Reviewed by a human, not inferred."""

    dependency_hashes: Mapped[Json]
    """Map of leg ticker to the terms hash current at approval time."""

    notes: Mapped[str | None] = mapped_column(Text)
    created_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    review_due_ts: Mapped[dt.datetime | None] = mapped_column()

    approvals: Mapped[list[Approval]] = relationship(
        back_populates="relationship_record", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_relationship_slug_version"),
        CheckConstraint("version > 0", name="version_positive"),
        Index("ix_relationship_status", "status"),
    )


class Approval(Base):
    """A reviewer's signed verdict on one relationship version.

    Immutable. Withdrawing an approval means recording a new decision, not
    editing the old one -- the question "who approved this, and what did they
    see" must remain answerable after the fact.
    """

    __tablename__ = "approval"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    relationship_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("relationship.id", ondelete="CASCADE")
    )
    reviewer: Mapped[str] = mapped_column(String(128))
    """Authenticated human identity. Never a model or a service account."""

    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision", native_enum=False)
    )
    decided_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    evidence: Mapped[str] = mapped_column(Text)
    """What the reviewer read: settlement wording, source URL, quoted terms."""

    scope: Mapped[Json]
    """Environments and quantity bounds this approval covers."""

    relationship_record: Mapped[RelationshipRecord] = relationship(back_populates="approvals")

    __table_args__ = (Index("ix_approval_relationship", "relationship_id", "decided_ts"),)


class AuditEvent(Base):
    """Append-only record of every decision, approval, and configuration change.

    ``prev_hash``/``hash`` chain the log so that a deleted or altered row is
    detectable rather than merely discouraged (NFR-05).
    """

    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    actor: Mapped[str] = mapped_column(String(128))
    """Human identity, service name, or "system". Attribution is mandatory."""

    action: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(128))
    detail: Mapped[Json]

    prev_hash: Mapped[Sha256 | None] = mapped_column()
    hash: Mapped[Sha256] = mapped_column()

    is_privileged: Mapped[bool] = mapped_column(Boolean, default=False)
    """Marks kill-switch, approval, and live-arming events for fast review."""

    __table_args__ = (
        Index("ix_audit_event_subject", "subject_type", "subject_id"),
        Index("ix_audit_event_occurred", "occurred_ts"),
    )
