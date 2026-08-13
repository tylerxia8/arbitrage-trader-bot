"""Reconstruction under adverse feed conditions.

Every scenario here is something a real WebSocket does on a bad day:
re-delivers messages after a reconnect, drops one, restarts its numbering.
The requirement is not that reconstruction survives these gracefully — it is
that it never produces a *plausible but wrong* book, because a book that is
three contracts too deep at one level looks exactly like a book that is
genuinely that deep, and the error surfaces as an arbitrage that does not fill.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arbbot.marketdata.reconstruct import ApplyOutcome, BookReconstructor
from arbbot.marketdata.types import (
    BookDelta,
    BookEvent,
    BookSide,
    DeltaEvent,
    PriceLevel,
    SnapshotEvent,
)

TICKER = "TEST-MARKET"


def snapshot(
    sequence: int, yes: dict[int, int] | None = None, no: dict[int, int] | None = None
) -> SnapshotEvent:
    levels = tuple(
        (side, PriceLevel(price, qty))
        for side, prices in ((BookSide.YES, yes or {}), (BookSide.NO, no or {}))
        for price, qty in prices.items()
    )
    return SnapshotEvent(ticker=TICKER, sequence=sequence, levels=levels)


def delta(sequence: int, side: BookSide, price: int, change: int) -> DeltaEvent:
    return DeltaEvent(ticker=TICKER, sequence=sequence, delta=BookDelta(side, price, change))


def drive(events: Iterable[BookEvent]) -> BookReconstructor:
    reconstructor = BookReconstructor(TICKER)
    for event in events:
        reconstructor.apply(event)
    return reconstructor


CLEAN_STREAM: list[BookEvent] = [
    snapshot(1, yes={40: 10}, no={55: 7}),
    delta(2, BookSide.YES, 40, +5),
    delta(3, BookSide.NO, 55, -2),
    delta(4, BookSide.YES, 41, +3),
]


class TestCleanStream:
    def test_applies_every_event(self) -> None:
        r = drive(CLEAN_STREAM)
        assert r.is_usable
        assert r.stats.applied == 4
        assert r.stats.invalidated == 0

    def test_produces_the_expected_book(self) -> None:
        r = drive(CLEAN_STREAM)
        assert r.book.bids(BookSide.YES) == [PriceLevel(41, 3), PriceLevel(40, 15)]
        assert r.book.bids(BookSide.NO) == [PriceLevel(55, 5)]


class TestDuplicates:
    def test_duplicate_is_discarded(self) -> None:
        r = drive([*CLEAN_STREAM, delta(4, BookSide.YES, 41, +3)])
        assert r.stats.duplicates == 1

    def test_duplicates_do_not_change_the_book(self) -> None:
        """The property that makes reconnects safe: re-delivery is idempotent."""
        clean = drive(CLEAN_STREAM)
        noisy = drive(
            [
                CLEAN_STREAM[0],
                CLEAN_STREAM[1],
                CLEAN_STREAM[1],
                CLEAN_STREAM[2],
                CLEAN_STREAM[2],
                CLEAN_STREAM[3],
            ]
        )
        assert noisy.book.checksum() == clean.book.checksum()

    @given(repeats=st.integers(min_value=1, max_value=6))
    def test_arbitrary_redelivery_is_idempotent(self, repeats: int) -> None:
        clean = drive(CLEAN_STREAM)
        noisy_stream: list[BookEvent] = []
        for event in CLEAN_STREAM:
            noisy_stream.extend([event] * repeats)
        assert drive(noisy_stream).book.checksum() == clean.book.checksum()


class TestDroppedMessages:
    def test_gap_invalidates_rather_than_guessing(self) -> None:
        """Deltas are signed changes, so a missing one corrupts every level
        that follows. There is no safe way to carry on."""
        r = drive([CLEAN_STREAM[0], CLEAN_STREAM[1], CLEAN_STREAM[3]])
        assert r.stats.invalidated == 1
        assert not r.is_usable

    def test_later_deltas_are_dropped_while_incomplete(self) -> None:
        r = drive(
            [
                snapshot(1, yes={40: 10}),
                delta(3, BookSide.YES, 40, +1),  # gap: 2 was lost
                delta(4, BookSide.YES, 40, +1),
                delta(5, BookSide.YES, 40, +1),
            ]
        )
        assert r.stats.invalidated == 1
        assert r.stats.dropped_incomplete == 2
        assert not r.is_usable

    def test_an_incomplete_book_refuses_to_be_priced(self) -> None:
        from arbbot.marketdata.book import BookIntegrityError

        r = drive([snapshot(1, no={55: 7}), delta(3, BookSide.NO, 55, +1)])
        with pytest.raises(BookIntegrityError):
            r.book.best_ask(BookSide.YES)


class TestRepair:
    def test_snapshot_repairs_an_invalidated_book(self) -> None:
        r = drive(
            [
                snapshot(1, yes={40: 10}),
                delta(3, BookSide.YES, 40, +1),  # gap
                snapshot(10, yes={42: 4}, no={50: 2}),
            ]
        )
        assert r.is_usable
        assert r.stats.repaired == 1
        assert r.book.bids(BookSide.YES) == [PriceLevel(42, 4)]

    def test_repaired_book_accepts_deltas_again(self) -> None:
        r = drive(
            [
                snapshot(1, yes={40: 10}),
                delta(3, BookSide.YES, 40, +1),  # gap
                snapshot(10, yes={42: 4}),
                delta(11, BookSide.YES, 42, +6),
            ]
        )
        assert r.is_usable
        assert r.book.bids(BookSide.YES) == [PriceLevel(42, 10)]

    def test_repair_discards_the_stale_levels(self) -> None:
        """Whatever the corrupted book held is exactly what must not survive."""
        r = drive(
            [
                snapshot(1, yes={40: 10}, no={55: 7}),
                delta(3, BookSide.YES, 40, +1),  # gap
                snapshot(10, yes={42: 4}),
            ]
        )
        assert r.book.bids(BookSide.NO) == []


class TestRewind:
    def test_venue_restarting_its_numbering_invalidates(self) -> None:
        r = drive(
            [
                snapshot(500, yes={40: 10}),
                delta(501, BookSide.YES, 40, +1),
                delta(1, BookSide.YES, 40, +1),
            ]
        )
        assert r.stats.invalidated == 1
        assert not r.is_usable

    def test_snapshot_after_rewind_recovers(self) -> None:
        r = drive(
            [
                snapshot(500, yes={40: 10}),
                delta(1, BookSide.YES, 40, +1),  # rewind
                snapshot(2, yes={7: 1}),
                delta(3, BookSide.YES, 7, +1),
            ]
        )
        assert r.is_usable
        assert r.book.bids(BookSide.YES) == [PriceLevel(7, 2)]


class TestMalformedEvents:
    def test_impossible_price_is_rejected_without_killing_the_stream(self) -> None:
        r = drive(
            [snapshot(1, yes={40: 10}), DeltaEvent(TICKER, 2, BookDelta(BookSide.YES, 100, +1))]
        )
        assert r.stats.rejected == 1

    def test_oversized_removal_invalidates_the_book(self) -> None:
        r = drive([snapshot(1, yes={40: 10}), delta(2, BookSide.YES, 40, -50)])
        assert r.stats.rejected == 1
        assert not r.is_usable


class TestDeterminism:
    def test_the_same_stream_always_yields_the_same_book(self) -> None:
        """NFR-03. Without this, replay proves nothing."""
        assert drive(CLEAN_STREAM).book.checksum() == drive(CLEAN_STREAM).book.checksum()

    @given(
        changes=st.lists(
            st.tuples(
                st.sampled_from([BookSide.YES, BookSide.NO]),
                st.integers(min_value=1, max_value=99),
                st.integers(min_value=1, max_value=20),
            ),
            min_size=1,
            max_size=30,
        )
    )
    def test_replaying_any_stream_reproduces_it_exactly(
        self, changes: list[tuple[BookSide, int, int]]
    ) -> None:
        stream: list[BookEvent] = [snapshot(1)]
        stream += [
            delta(i, side, price, qty) for i, (side, price, qty) in enumerate(changes, start=2)
        ]

        first = drive(stream)
        second = drive(stream)
        assert first.book.checksum() == second.book.checksum()
        assert first.is_usable == second.is_usable

    def test_outcomes_that_advance_the_book_are_exactly_applied_and_repaired(self) -> None:
        advancing = {o for o in ApplyOutcome if o.advanced_book}
        assert advancing == {ApplyOutcome.APPLIED, ApplyOutcome.REPAIRED}
