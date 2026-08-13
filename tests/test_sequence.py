"""Sequence classification and its memory bound."""

from __future__ import annotations

from arbbot.marketdata.sequence import DUPLICATE_WINDOW, SequenceTracker, SequenceVerdict


class TestClassification:
    def test_first_message(self) -> None:
        assert SequenceTracker().observe(100) is SequenceVerdict.FIRST

    def test_successor_is_in_order(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(1)
        assert tracker.observe(2) is SequenceVerdict.IN_ORDER

    def test_repeat_is_a_duplicate(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(1)
        tracker.observe(2)
        assert tracker.observe(2) is SequenceVerdict.DUPLICATE
        assert tracker.duplicates == 1

    def test_forward_jump_is_a_gap(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(1)
        assert tracker.observe(5) is SequenceVerdict.GAP
        assert tracker.gaps == 1

    def test_unseen_lower_value_is_a_rewind(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(100)
        tracker.observe(101)
        assert tracker.observe(1) is SequenceVerdict.REWIND
        assert tracker.rewinds == 1

    def test_rewind_restarts_the_stream(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(100)
        tracker.observe(1)
        assert tracker.observe(2) is SequenceVerdict.IN_ORDER


class TestMissingCount:
    def test_counts_skipped_messages_not_just_gap_events(self) -> None:
        """One gap of 500 and 500 gaps of one are very different feed
        conditions and must not aggregate to the same number."""
        one_big = SequenceTracker()
        one_big.observe(1)
        one_big.observe(501)

        many_small = SequenceTracker()
        many_small.observe(1)
        for n in range(3, 1002, 2):
            many_small.observe(n)

        assert one_big.gaps == 1
        assert one_big.missing_messages == 499
        assert many_small.gaps == 500
        assert many_small.missing_messages == 500


class TestVerdictPolicy:
    def test_only_first_and_in_order_may_be_applied(self) -> None:
        applicable = {v for v in SequenceVerdict if v.is_applicable}
        assert applicable == {SequenceVerdict.FIRST, SequenceVerdict.IN_ORDER}

    def test_gap_and_rewind_invalidate_the_book(self) -> None:
        invalidating = {v for v in SequenceVerdict if v.invalidates_book}
        assert invalidating == {SequenceVerdict.GAP, SequenceVerdict.REWIND}

    def test_a_duplicate_does_not_invalidate(self) -> None:
        """Re-delivery after a reconnect is routine, not a data loss event."""
        assert not SequenceVerdict.DUPLICATE.invalidates_book


class TestBoundedMemory:
    def test_recent_window_does_not_grow_without_limit(self) -> None:
        """The exit gate is seven days of continuous collection. An unbounded
        set of every sequence ever seen would exhaust memory long before that,
        across every subscribed market at once."""
        tracker = SequenceTracker()
        for n in range(1, DUPLICATE_WINDOW * 3):
            tracker.observe(n)

        assert len(tracker._recent) <= DUPLICATE_WINDOW
        assert len(tracker._recent_set) <= DUPLICATE_WINDOW

    def test_recent_duplicates_are_still_caught(self) -> None:
        tracker = SequenceTracker()
        for n in range(1, DUPLICATE_WINDOW * 2):
            tracker.observe(n)
        recent = DUPLICATE_WINDOW * 2 - 2
        assert tracker.observe(recent) is SequenceVerdict.DUPLICATE

    def test_a_repeat_older_than_the_window_fails_closed(self) -> None:
        """Classified as a rewind, which invalidates and resubscribes. Wrong
        in a safe direction: the alternative is discarding a message that
        mattered because it looked old."""
        tracker = SequenceTracker()
        for n in range(1, DUPLICATE_WINDOW * 2):
            tracker.observe(n)
        assert tracker.observe(1) is SequenceVerdict.REWIND


class TestReset:
    def test_reset_clears_history_but_keeps_counters(self) -> None:
        """Counters describe the session's health, not the current socket."""
        tracker = SequenceTracker()
        tracker.observe(1)
        tracker.observe(5)
        assert tracker.gaps == 1

        tracker.reset()
        assert tracker.last_sequence is None
        assert tracker.gaps == 1
        assert tracker.observe(42) is SequenceVerdict.FIRST

    def test_anomalies_aggregates_all_three_kinds(self) -> None:
        tracker = SequenceTracker()
        tracker.observe(1)
        tracker.observe(5)  # gap
        tracker.observe(5)  # duplicate
        tracker.observe(2)  # rewind
        assert tracker.anomalies == 3
