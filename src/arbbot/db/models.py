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

from arbbot.db.base import (
    Base,
    BigIntPk,
    Json,
    JsonList,
    JsonRows,
    Money,
    Sha256,
    Timestamp,
)
from arbbot.relationships import ApprovalDecision, RelationshipStatus, RelationshipType

__all__ = [
    "Approval",
    "AuditEvent",
    "Evaluation",
    "Market",
    "PollCycle",
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

    id: Mapped[BigIntPk]
    venue: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(128))
    """REST endpoint path or WebSocket channel name."""

    subscription_key: Mapped[str | None] = mapped_column(String(160))
    """Identity of the stream a sequence number belongs to, e.g.
    ``orderbook_delta:KXBTC-25DEC31``. Sequence numbers are per-subscription,
    so without this a message from one market collides with the same sequence
    number from another."""

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
        Index("ix_raw_message_venue_sequence", "venue", "subscription_key", "sequence"),
        # Identity is (stream, sequence) -- the venue's own statement of "this
        # is message N of this subscription" -- not payload content. Two
        # heartbeats a minute apart are byte-identical and both real; keying
        # dedupe on content would discard the second as though it never
        # happened. Rows with a NULL sequence never collide, because NULL is
        # distinct from NULL in a unique index, so unsequenced messages are
        # always stored.
        UniqueConstraint(
            "venue", "subscription_key", "sequence", name="uq_raw_message_stream_sequence"
        ),
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

    legs: Mapped[JsonRows]
    """Leg definitions: venue, ticker, side, and quantity ratio per leg.

    An array of objects, not a mapping -- typed as such so the checker knows
    it. Annotating a list column as a dict makes every membership and index
    check against it silently meaningless.
    """

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


class BookSnapshot(Base):
    """Reconstructed book state at a point in time.

    A derived artifact, not evidence: it can always be rebuilt by replaying
    the raw archive. It exists so that detection does not have to replay from
    the beginning of time, and so that a stored ``checksum`` lets replay assert
    it reproduced exactly the state that was acted on (FR-001).

    ``raw_message_id`` links the snapshot to the exact message that produced
    it, which is what makes an evaluation traceable to its inputs (FR-003).
    """

    __tablename__ = "book_snapshot"

    id: Mapped[BigIntPk]
    venue: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(128))
    captured_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    sequence: Mapped[int | None] = mapped_column(BigInteger)

    yes_levels: Mapped[Json]
    no_levels: Mapped[Json]
    """Resting bids per side as ``{price_cents: quantity}``. Stored as the
    venue quotes them; executable asks are derived, never persisted, so the
    record cannot drift from what was actually reported."""

    checksum: Mapped[Sha256]
    is_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    """False if a gap or integrity failure left the book unusable. An
    incomplete snapshot is kept for diagnostics and must never be priced."""

    raw_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_message.id", ondelete="RESTRICT")
    )
    """RESTRICT, not CASCADE: deleting archived evidence that a snapshot
    depends on must fail loudly rather than quietly orphan the derivation."""

    __table_args__ = (
        Index("ix_book_snapshot_market_captured", "venue", "ticker", "captured_ts"),
        Index("ix_book_snapshot_sequence", "venue", "ticker", "sequence"),
    )


class FeedHealth(Base):
    """Periodic health sample for one subscription stream.

    NFR-01 forbids a silent outage longer than two minutes, which is only
    enforceable if the absence of data is itself recorded. Sampling on a timer
    means a stopped collector leaves a visible hole in this table rather than
    simply writing nothing anywhere.
    """

    __tablename__ = "feed_health"

    id: Mapped[BigIntPk]
    observed_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    venue: Mapped[str] = mapped_column(String(32))
    subscription_key: Mapped[str] = mapped_column(String(160))

    messages: Mapped[int] = mapped_column(BigInteger, default=0)
    gaps: Mapped[int] = mapped_column(Integer, default=0)
    missing_messages: Mapped[int] = mapped_column(BigInteger, default=0)
    """Skipped sequence numbers, not gap events. One gap of 500 and 500 gaps
    of one are very different conditions and must not aggregate together."""

    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    rewinds: Mapped[int] = mapped_column(Integer, default=0)
    reconnects: Mapped[int] = mapped_column(Integer, default=0)
    parse_errors: Mapped[int] = mapped_column(Integer, default=0)

    last_message_ts: Mapped[dt.datetime | None] = mapped_column()
    lag_ms: Mapped[int | None] = mapped_column(Integer)
    """Age of the most recent message at sample time. Measured, never assumed
    (NFR-10)."""

    is_healthy: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_feed_health_stream_observed", "venue", "subscription_key", "observed_ts"),
    )


class VenueLease(Base):
    """A claim on part of one venue's request budget.

    The venue rate-limits per IP, and this system rate-limits per *component*.
    Those are not the same denominator, and on 2026-08-14 the difference cost
    the project its access: a collector at four requests a second, a one-second
    probe at six, a proposal sweep, and a venue-wide survey at five were each
    individually well under the ceiling, ran at once, and summed to well over
    it. The production host began resetting TLS handshakes and fifteen hours of
    collection were lost.

    So a budget that is enforced per process is not enforced at all. Every
    consumer records its claimed rate here before its first request, and a
    consumer whose rate would push the live total past the venue ceiling
    refuses to start rather than quietly making the sum somebody else's
    problem. The database is the only thing all of them share -- the collector
    runs in a container, the probe and the survey run on the host -- so this is
    where the arithmetic has to live.

    Leases are heartbeated rather than held, because the failure that matters
    is a consumer dying without releasing. A stale lease expires and its share
    returns to the pool; a lease that could only be released cleanly would let
    one crash lock out the venue until someone noticed.
    """

    __tablename__ = "venue_lease"

    id: Mapped[BigIntPk]
    venue: Mapped[str] = mapped_column(String(32))
    consumer: Mapped[str] = mapped_column(String(64))
    """What is spending the budget: ``collector``, ``probe``, ``survey``."""

    requests_per_second: Mapped[int] = mapped_column(Integer)
    owner: Mapped[str] = mapped_column(String(128))
    """Host and process id, so a stale lease can be traced to what left it."""

    started_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    heartbeat_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())

    __table_args__ = (Index("ix_venue_lease_venue_heartbeat", "venue", "heartbeat_ts"),)


class PollCycle(Base):
    """One pass of a poller over its markets, and which of them it confirmed.

    This exists because :class:`BookSnapshot` records when a book *changed*,
    not when it was *observed*. Unchanged polls are deliberately not
    re-archived -- storing thousands of byte-identical books would bloat the
    archive without adding evidence -- and the consequence was invisible until
    the fast-poll probe ran: an analysis reading ``captured_ts`` as the quote's
    age charges a leg for every second since it last moved, even while a poller
    was confirming it current every second.

    That is not a small distortion. On a one-second probe of a live temperature
    partition, the median gap between changes was three to four seconds and the
    longest ran past twelve minutes, so a two-second freshness gate rejected
    almost everything on staleness that was in fact freshly confirmed. Any
    finding of the form "the edge was gone before we could see it" is
    uninterpretable without this table.

    One row per cycle rather than per market: a cycle is the unit that confirms,
    the tickers ride along as an array, and a seven-day run costs thousands of
    rows instead of hundreds of thousands.

    Append-only, like the raw archive. A mutable "last confirmed" column would
    be smaller and would make replay a lie -- evaluating the archive at a past
    moment would read a confirmation from that moment's future.
    """

    __tablename__ = "poll_cycle"

    id: Mapped[BigIntPk]
    venue: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(64))
    """Which poller. The 1s probe and the 30s collector confirm independently
    and must not be read as one stream (see ``collector.PROBE_CHANNEL``)."""

    started_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    completed_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())
    """When the cycle finished. Freshness is measured from this rather than
    from ``started_ts``: a leg polled at the end of a slow cycle was confirmed
    then, and crediting it with the cycle's start time would overstate how
    fresh it was -- the direction that invents edge."""

    confirmed: Mapped[JsonList] = mapped_column(default=list)
    """Tickers this cycle polled successfully, changed or not."""

    failed: Mapped[JsonList] = mapped_column(default=list)
    """Tickers whose poll failed. Recorded rather than omitted, so a silent
    absence from ``confirmed`` cannot be mistaken for a market that was simply
    not in the universe yet."""

    __table_args__ = (Index("ix_poll_cycle_channel_completed", "venue", "channel", "completed_ts"),)


class Evaluation(Base):
    """One pricing decision, accepted or rejected, with its inputs.

    **Rejections are persisted too**, and that is the point. "Nothing
    qualified today" is a useless sentence and a countable set of reason codes
    is a finding, so the falsification report is built from this table rather
    than from the acceptances alone -- which would be a table of successes
    with no denominator.

    Every row carries the versions it was decided under (FR-003, NFR-03). When
    the fee model or the parser turns out to have been wrong, this is what
    makes the past re-derivable instead of merely regrettable.
    """

    __tablename__ = "evaluation"

    id: Mapped[BigIntPk]
    evaluated_ts: Mapped[Timestamp] = mapped_column(server_default=func.now())

    relationship_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("relationship.id", ondelete="RESTRICT")
    )
    """RESTRICT: a relationship with decisions behind it cannot be deleted out
    from under them."""

    relationship_slug: Mapped[str] = mapped_column(String(128))
    relationship_version: Mapped[int] = mapped_column(Integer)

    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    """Rejection reason code, from the closed catalog. NULL when accepted."""

    detail: Mapped[str | None] = mapped_column(Text)

    quantity: Mapped[Money]
    acquisition_cost: Mapped[Money]
    fees: Mapped[Money]
    reserves: Mapped[Money]
    guaranteed_payout: Mapped[Money]
    net_edge: Mapped[Money]

    legs: Mapped[Json]
    """Per-leg quotes and depth walks, so the decision reproduces exactly."""

    fee_rule: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(32))
    max_book_age_ms: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_evaluation_slug_ts", "relationship_slug", "evaluated_ts"),
        Index("ix_evaluation_reason", "reason", "evaluated_ts"),
        Index("ix_evaluation_accepted", "accepted", "evaluated_ts"),
    )


class AuditEvent(Base):
    """Append-only record of every decision, approval, and configuration change.

    ``prev_hash``/``hash`` chain the log so that a deleted or altered row is
    detectable rather than merely discouraged (NFR-05).
    """

    __tablename__ = "audit_event"

    id: Mapped[BigIntPk]
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
