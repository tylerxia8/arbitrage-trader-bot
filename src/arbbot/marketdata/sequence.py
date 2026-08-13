"""Sequence tracking for a single subscription stream.

Separate from :class:`~arbbot.marketdata.book.OrderBook`, which enforces
contiguity as a precondition of applying a delta. This classifies *why* a
sequence was unexpected, which the book does not need to know but the health
metrics and the operator do: a duplicate after a reconnect is routine, a gap
means data was lost, and a rewind means the venue restarted the stream.

Distinguishing them matters because the responses differ. A duplicate is
discarded silently. A gap invalidates the book and requires a resubscribe. A
rewind means the venue's own numbering restarted, so the old book is not
merely stale but meaningless.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field
from typing import Final

__all__ = ["DUPLICATE_WINDOW", "SequenceTracker", "SequenceVerdict"]

#: How many recent sequence numbers are remembered for duplicate detection.
#:
#: Bounded deliberately. The exit gate for this milestone is seven days of
#: continuous collection, and an unbounded set of every sequence number ever
#: seen would grow without limit across every subscribed market -- the
#: collector would die of memory exhaustion somewhere around day three, which
#: is the least useful moment to discover the problem.
#:
#: Duplicates in practice arrive in the burst immediately after a reconnect,
#: well inside this window. A repeat older than the window is classified as a
#: rewind, which fails closed: it invalidates the book and forces a
#: resubscribe rather than silently discarding a message that mattered.
DUPLICATE_WINDOW: Final = 4096


class SequenceVerdict(enum.StrEnum):
    """Classification of an observed sequence number."""

    FIRST = "first"
    """Opening message of the stream; nothing to compare against."""

    IN_ORDER = "in_order"
    """The immediate successor. The only case where a delta may be applied."""

    DUPLICATE = "duplicate"
    """Already seen. Expected after a reconnect; discard without applying."""

    GAP = "gap"
    """Jumped forward -- messages were lost. The book must be invalidated."""

    REWIND = "rewind"
    """Went backwards without repeating a seen value: the venue restarted its
    numbering. The existing book is meaningless, not merely stale."""

    @property
    def is_applicable(self) -> bool:
        """Whether a message with this verdict may be applied to a book."""
        return self in (SequenceVerdict.FIRST, SequenceVerdict.IN_ORDER)

    @property
    def invalidates_book(self) -> bool:
        """Whether observing this must mark the book incomplete."""
        return self in (SequenceVerdict.GAP, SequenceVerdict.REWIND)


@dataclass(slots=True)
class SequenceTracker:
    """Classifies sequence numbers on one stream and counts anomalies."""

    last_sequence: int | None = None
    highest_sequence: int | None = None

    gaps: int = 0
    duplicates: int = 0
    rewinds: int = 0
    missing_messages: int = 0
    """Total count of skipped sequence numbers, not just the number of gap
    events. One gap of 500 and 500 gaps of one are very different feed
    conditions and must not aggregate to the same number."""

    _recent: deque[int] = field(default_factory=lambda: deque(maxlen=DUPLICATE_WINDOW), repr=False)
    _recent_set: set[int] = field(default_factory=set, repr=False)

    def observe(self, sequence: int) -> SequenceVerdict:
        """Classify ``sequence`` and update counters."""
        if self.last_sequence is None:
            self._remember(sequence)
            self.last_sequence = sequence
            self.highest_sequence = sequence
            return SequenceVerdict.FIRST

        if sequence in self._recent_set:
            self.duplicates += 1
            return SequenceVerdict.DUPLICATE

        if sequence == self.last_sequence + 1:
            self._remember(sequence)
            self.last_sequence = sequence
            self.highest_sequence = max(self.highest_sequence or sequence, sequence)
            return SequenceVerdict.IN_ORDER

        if sequence > self.last_sequence:
            self.gaps += 1
            self.missing_messages += sequence - self.last_sequence - 1
            self._remember(sequence)
            self.last_sequence = sequence
            self.highest_sequence = max(self.highest_sequence or sequence, sequence)
            return SequenceVerdict.GAP

        # Below the last value and not in the recent window: the stream
        # restarted lower, or the repeat is older than we track. Both are
        # handled the same way -- invalidate and resubscribe -- because
        # guessing which one it was cannot be done from the number alone.
        self.rewinds += 1
        self.reset(sequence)
        return SequenceVerdict.REWIND

    def reset(self, sequence: int | None = None) -> None:
        """Forget stream history, keeping the anomaly counters.

        Called on resubscribe and on rewind. Counters survive because they are
        health metrics for the session, not properties of the current stream.
        """
        self.last_sequence = sequence
        self.highest_sequence = sequence
        self._recent.clear()
        self._recent_set.clear()
        if sequence is not None:
            self._remember(sequence)

    def _remember(self, sequence: int) -> None:
        """Record a sequence, evicting the oldest to stay within the window."""
        if len(self._recent) == self._recent.maxlen:
            self._recent_set.discard(self._recent[0])
        self._recent.append(sequence)
        self._recent_set.add(sequence)

    @property
    def anomalies(self) -> int:
        """Total anomalous observations, for a single at-a-glance metric."""
        return self.gaps + self.duplicates + self.rewinds
