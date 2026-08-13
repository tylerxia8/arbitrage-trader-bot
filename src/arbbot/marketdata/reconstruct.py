"""Book reconstruction: the one code path from events to book state.

Live collection and archive replay both drive a :class:`BookReconstructor`.
That is deliberate and load-bearing. FR-001 requires that raw records replay
to an identical book state, and an equivalence test only means something if
both routes execute the same logic — two implementations that agree on the
fixtures someone thought to write are not the same as one implementation
exercised twice.

This layer owns the policy that :class:`~arbbot.marketdata.book.OrderBook`
deliberately does not: what to *do* about an anomalous sequence. The book
enforces contiguity as a precondition; the tracker classifies the anomaly;
this decides that duplicates are discarded, gaps invalidate and wait for a
snapshot, and rewinds start over.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from arbbot.marketdata.book import BookIntegrityError, OrderBook
from arbbot.marketdata.sequence import SequenceTracker, SequenceVerdict
from arbbot.marketdata.types import BookEvent, DeltaEvent, SnapshotEvent

__all__ = ["ApplyOutcome", "BookReconstructor", "ReconstructionStats"]


class ApplyOutcome(enum.StrEnum):
    """What happened to one event."""

    APPLIED = "applied"
    """Moved the book forward."""

    REPAIRED = "repaired"
    """A snapshot restored an incomplete book."""

    DISCARDED_DUPLICATE = "discarded_duplicate"
    """Already seen; re-delivered after a reconnect."""

    INVALIDATED = "invalidated"
    """A gap or rewind lost data. The book is now incomplete."""

    DROPPED_INCOMPLETE = "dropped_incomplete"
    """A delta arrived while the book was incomplete. Cannot be applied; the
    stream must wait for a snapshot."""

    REJECTED = "rejected"
    """The event was structurally invalid -- an impossible price, or a change
    that would leave negative size resting."""

    @property
    def advanced_book(self) -> bool:
        return self in (ApplyOutcome.APPLIED, ApplyOutcome.REPAIRED)


@dataclass(slots=True)
class ReconstructionStats:
    """Counts for health reporting and replay assertions."""

    events: int = 0
    applied: int = 0
    repaired: int = 0
    duplicates: int = 0
    invalidated: int = 0
    dropped_incomplete: int = 0
    rejected: int = 0

    def record(self, outcome: ApplyOutcome) -> None:
        self.events += 1
        match outcome:
            case ApplyOutcome.APPLIED:
                self.applied += 1
            case ApplyOutcome.REPAIRED:
                self.repaired += 1
            case ApplyOutcome.DISCARDED_DUPLICATE:
                self.duplicates += 1
            case ApplyOutcome.INVALIDATED:
                self.invalidated += 1
            case ApplyOutcome.DROPPED_INCOMPLETE:
                self.dropped_incomplete += 1
            case ApplyOutcome.REJECTED:
                self.rejected += 1


@dataclass(slots=True)
class BookReconstructor:
    """Drives one market's book from a stream of events."""

    ticker: str
    book: OrderBook = field(init=False)
    tracker: SequenceTracker = field(init=False, default_factory=SequenceTracker)
    stats: ReconstructionStats = field(init=False, default_factory=ReconstructionStats)
    _ever_complete: bool = field(init=False, default=False)
    """Whether the book has ever been usable. Distinguishes the opening
    snapshot from a snapshot that recovered a broken stream -- otherwise every
    healthy feed reports one repair it never needed, and the metric that is
    supposed to reveal trouble is noise from the first message onward."""

    def __post_init__(self) -> None:
        self.book = OrderBook(self.ticker)

    def apply(self, event: BookEvent) -> ApplyOutcome:
        """Apply one event and report what happened."""
        outcome = self._apply(event)
        self.stats.record(outcome)
        return outcome

    def _apply(self, event: BookEvent) -> ApplyOutcome:
        verdict = self.tracker.observe(event.sequence)

        if verdict is SequenceVerdict.DUPLICATE:
            return ApplyOutcome.DISCARDED_DUPLICATE

        if isinstance(event, SnapshotEvent):
            # A snapshot is self-contained, so it repairs the book regardless
            # of what the sequence did. This is the only route out of an
            # incomplete book.
            recovering = not self.book.is_complete and self._ever_complete
            try:
                self.book.apply_snapshot(event.levels, event.sequence)
            except BookIntegrityError:
                self.book.invalidate()
                return ApplyOutcome.REJECTED
            self.tracker.reset(event.sequence)
            self._ever_complete = True
            return ApplyOutcome.REPAIRED if recovering else ApplyOutcome.APPLIED

        if verdict.invalidates_book:
            self.book.invalidate()
            return ApplyOutcome.INVALIDATED

        if not self.book.is_complete:
            return ApplyOutcome.DROPPED_INCOMPLETE

        assert isinstance(event, DeltaEvent)  # noqa: S101 -- exhaustive union check
        try:
            self.book.apply_delta(event.delta, event.sequence)
        except BookIntegrityError:
            return ApplyOutcome.REJECTED
        return ApplyOutcome.APPLIED

    @property
    def is_usable(self) -> bool:
        """Whether the book may currently be priced."""
        return self.book.is_complete
