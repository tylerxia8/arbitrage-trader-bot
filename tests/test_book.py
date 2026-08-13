"""Order-book reconstruction mechanics.

The ask-derivation tests carry the most weight. The venue quotes resting bids
on both outcomes, and misreading that -- treating a YES bid as a YES ask --
would make every basket look about twice as cheap as it is, which is exactly
the shape of a bug that presents as a spectacular arbitrage opportunity.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arbbot.marketdata.book import BookIntegrityError, OrderBook
from arbbot.marketdata.types import BookDelta, BookSide, PriceLevel


def build(levels: dict[BookSide, dict[int, int]], sequence: int = 1) -> OrderBook:
    book = OrderBook("TEST-MARKET")
    book.apply_snapshot(
        [
            (side, PriceLevel(price, qty))
            for side, prices in levels.items()
            for price, qty in prices.items()
        ],
        sequence=sequence,
    )
    return book


class TestSnapshot:
    def test_starts_incomplete(self) -> None:
        """Nothing may be priced before the first snapshot arrives."""
        book = OrderBook("TEST")
        assert not book.is_complete
        with pytest.raises(BookIntegrityError, match="incomplete"):
            book.best_ask(BookSide.YES)

    def test_snapshot_makes_the_book_usable(self) -> None:
        book = build({BookSide.YES: {40: 10}, BookSide.NO: {55: 7}})
        assert book.is_complete
        assert book.sequence == 1

    def test_snapshot_replaces_rather_than_merges(self) -> None:
        """A snapshot is the repair path, so it must not carry stale levels
        forward -- that is precisely what a corrupted book needs discarded."""
        book = build({BookSide.YES: {40: 10}})
        book.apply_snapshot([(BookSide.YES, PriceLevel(60, 3))], sequence=9)
        assert book.bids(BookSide.YES) == [PriceLevel(60, 3)]

    def test_zero_quantity_levels_are_dropped(self) -> None:
        book = build({BookSide.YES: {40: 0, 41: 5}})
        assert book.bids(BookSide.YES) == [PriceLevel(41, 5)]

    @pytest.mark.parametrize("price", [0, 100, -1, 101])
    def test_impossible_prices_are_rejected(self, price: int) -> None:
        """A resting bid at 0 or 100 is a resolved market, not a bargain."""
        book = OrderBook("TEST")
        with pytest.raises(BookIntegrityError, match="outside the open interval"):
            book.apply_snapshot([(BookSide.YES, PriceLevel(price, 1))], sequence=1)


class TestAskDerivation:
    def test_yes_asks_come_from_no_bids(self) -> None:
        """A NO bid at 55c is an offer to sell YES at 45c."""
        book = build({BookSide.NO: {55: 7}})
        assert book.best_ask(BookSide.YES) == PriceLevel(45, 7)

    def test_no_asks_come_from_yes_bids(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        assert book.best_ask(BookSide.NO) == PriceLevel(60, 10)

    def test_best_no_bid_becomes_the_cheapest_yes_ask(self) -> None:
        """Descending bids map to ascending asks; getting this backwards would
        price every basket off the *worst* available level."""
        book = build({BookSide.NO: {55: 7, 50: 3, 58: 1}})
        assert book.ask_levels(BookSide.YES) == [
            PriceLevel(42, 1),
            PriceLevel(45, 7),
            PriceLevel(50, 3),
        ]

    def test_a_side_with_no_opposing_bids_has_no_ask(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        assert book.best_ask(BookSide.YES) is None

    def test_own_side_bids_are_not_asks(self) -> None:
        """The bug this guards: reading a YES bid at 40c as a YES ask at 40c
        makes a basket look far cheaper than it can actually be bought for."""
        book = build({BookSide.YES: {40: 10}, BookSide.NO: {55: 7}})
        yes_ask = book.best_ask(BookSide.YES)
        assert yes_ask is not None
        assert yes_ask.price_cents == 45
        assert yes_ask.price_cents != 40

    @given(
        yes_bid=st.integers(min_value=1, max_value=99),
        no_bid=st.integers(min_value=1, max_value=99),
    )
    def test_ask_and_bid_are_complements(self, yes_bid: int, no_bid: int) -> None:
        book = build({BookSide.YES: {yes_bid: 1}, BookSide.NO: {no_bid: 1}})
        yes_ask = book.best_ask(BookSide.YES)
        no_ask = book.best_ask(BookSide.NO)
        assert yes_ask is not None
        assert no_ask is not None
        assert yes_ask.price_cents == 100 - no_bid
        assert no_ask.price_cents == 100 - yes_bid


class TestDeltas:
    def test_delta_adds_size(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        book.apply_delta(BookDelta(BookSide.YES, 40, +5), sequence=2)
        assert book.bids(BookSide.YES) == [PriceLevel(40, 15)]

    def test_delta_removes_size(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        book.apply_delta(BookDelta(BookSide.YES, 40, -4), sequence=2)
        assert book.bids(BookSide.YES) == [PriceLevel(40, 6)]

    def test_delta_to_zero_removes_the_level(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        book.apply_delta(BookDelta(BookSide.YES, 40, -10), sequence=2)
        assert book.bids(BookSide.YES) == []

    def test_delta_creates_a_new_level(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        book.apply_delta(BookDelta(BookSide.YES, 41, +3), sequence=2)
        assert book.bids(BookSide.YES) == [PriceLevel(41, 3), PriceLevel(40, 10)]

    def test_delta_below_zero_invalidates_the_book(self) -> None:
        """Local state has diverged from the venue. The book cannot be trusted
        to be merely 'a bit off' -- every later delta compounds the error."""
        book = build({BookSide.YES: {40: 10}})
        with pytest.raises(BookIntegrityError, match="diverged"):
            book.apply_delta(BookDelta(BookSide.YES, 40, -11), sequence=2)
        assert not book.is_complete

    def test_delta_on_incomplete_book_is_refused(self) -> None:
        book = OrderBook("TEST")
        with pytest.raises(BookIntegrityError, match="await a snapshot"):
            book.apply_delta(BookDelta(BookSide.YES, 40, +1), sequence=2)

    def test_sequence_gap_invalidates_the_book(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        with pytest.raises(BookIntegrityError, match="sequence gap"):
            book.apply_delta(BookDelta(BookSide.YES, 40, +1), sequence=5)
        assert not book.is_complete

    def test_book_stays_unusable_until_a_snapshot(self) -> None:
        book = build({BookSide.YES: {40: 10}})
        with pytest.raises(BookIntegrityError):
            book.apply_delta(BookDelta(BookSide.YES, 40, +1), sequence=5)

        with pytest.raises(BookIntegrityError, match="incomplete"):
            book.best_ask(BookSide.NO)

        book.apply_snapshot([(BookSide.YES, PriceLevel(40, 2))], sequence=6)
        assert book.is_complete


class TestChecksum:
    def test_same_levels_produce_the_same_checksum(self) -> None:
        a = build({BookSide.YES: {40: 10}, BookSide.NO: {55: 7}}, sequence=1)
        b = build({BookSide.YES: {40: 10}, BookSide.NO: {55: 7}}, sequence=99)
        assert a.checksum() == b.checksum()

    def test_checksum_ignores_sequence_but_not_state(self) -> None:
        """Two books reaching the same levels by different routes must compare
        equal; that is the whole basis of the replay-equivalence check."""
        a = build({BookSide.YES: {40: 10}})
        b = build({BookSide.YES: {40: 8}})
        b.apply_delta(BookDelta(BookSide.YES, 40, +2), sequence=2)
        assert a.checksum() == b.checksum()

    def test_different_levels_produce_different_checksums(self) -> None:
        a = build({BookSide.YES: {40: 10}})
        b = build({BookSide.YES: {40: 11}})
        assert a.checksum() != b.checksum()

    def test_side_placement_changes_the_checksum(self) -> None:
        """Same price and size on the wrong side is a different book."""
        a = build({BookSide.YES: {40: 10}})
        b = build({BookSide.NO: {40: 10}})
        assert a.checksum() != b.checksum()
