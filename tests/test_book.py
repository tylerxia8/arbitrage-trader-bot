"""Order-book reconstruction mechanics.

Two groups carry the most weight.

The ask-derivation tests: the venue quotes resting bids on *both* outcomes,
and misreading that -- treating a YES bid as a YES ask -- would make every
basket look about twice as cheap as it is, which presents as a spectacular
arbitrage rather than as a bug.

The precision tests: prices are dollar strings with up to four decimals (tick
size is per-market, and ``deci_cent`` markets really do quote $0.001 steps)
and contract counts are fractional -- a live book shows sizes like ``809.25``.
Rounding either to whole units was the model this milestone started with, and
it was wrong.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arbbot.marketdata.book import BookIntegrityError, OrderBook
from arbbot.marketdata.types import BookDelta, BookSide, PriceLevel

D = Decimal


def build(levels: dict[BookSide, dict[str, str]], sequence: int = 1) -> OrderBook:
    """Build a book from ``{side: {price_string: quantity_string}}``."""
    book = OrderBook("TEST-MARKET")
    book.apply_snapshot(
        [
            (side, PriceLevel(D(price), D(qty)))
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
        book = build({BookSide.YES: {"0.40": "10"}, BookSide.NO: {"0.55": "7"}})
        assert book.is_complete
        assert book.sequence == 1

    def test_snapshot_replaces_rather_than_merges(self) -> None:
        """A snapshot is the repair path, so it must not carry stale levels
        forward -- that is precisely what a corrupted book needs discarded."""
        book = build({BookSide.YES: {"0.40": "10"}})
        book.apply_snapshot([(BookSide.YES, PriceLevel(D("0.60"), D("3")))], sequence=9)
        assert book.bids(BookSide.YES) == [PriceLevel(D("0.60"), D("3"))]

    def test_zero_quantity_levels_are_dropped(self) -> None:
        book = build({BookSide.YES: {"0.40": "0", "0.41": "5"}})
        assert book.bids(BookSide.YES) == [PriceLevel(D("0.41"), D("5"))]

    @pytest.mark.parametrize("price", ["0", "1.00", "-0.01", "1.01"])
    def test_impossible_prices_are_rejected(self, price: str) -> None:
        """A resting bid at $0 or $1 is a resolved market, not a bargain."""
        book = OrderBook("TEST")
        with pytest.raises(BookIntegrityError, match="outside the tradeable range"):
            book.apply_snapshot([(BookSide.YES, PriceLevel(D(price), D("1")))], sequence=1)


class TestPrecision:
    def test_sub_cent_prices_survive(self) -> None:
        """deci_cent markets quote $0.001 steps. Truncating to whole cents
        would move the price by a full tick on every level."""
        book = build({BookSide.NO: {"0.5555": "10"}})
        ask = book.best_ask(BookSide.YES)
        assert ask is not None
        assert ask.price_dollars == D("0.4445")

    def test_fractional_quantities_survive(self) -> None:
        """Live books show sizes like 809.25; rounding to whole contracts
        misstates the depth every basket is priced against."""
        book = build({BookSide.NO: {"0.37": "809.25"}})
        ask = book.best_ask(BookSide.YES)
        assert ask is not None
        assert ask.quantity == D("809.25")

    def test_fractional_deltas_accumulate_exactly(self) -> None:
        book = build({BookSide.YES: {"0.40": "0.10"}})
        book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("0.10")), sequence=2)
        book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("0.10")), sequence=3)
        book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("0.10")), sequence=4)
        assert book.bids(BookSide.YES) == [PriceLevel(D("0.40"), D("0.40"))]

    def test_no_binary_float_error_creeps_in(self) -> None:
        """The whole reason money is Decimal: 0.1 + 0.2 must be exactly 0.3."""
        book = build({BookSide.YES: {"0.40": "0.10"}})
        book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("0.20")), sequence=2)
        assert book.bids(BookSide.YES)[0].quantity == D("0.30")


class TestAskDerivation:
    def test_yes_asks_come_from_no_bids(self) -> None:
        """A NO bid at $0.55 is an offer to sell YES at $0.45."""
        book = build({BookSide.NO: {"0.55": "7"}})
        assert book.best_ask(BookSide.YES) == PriceLevel(D("0.45"), D("7"))

    def test_no_asks_come_from_yes_bids(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        assert book.best_ask(BookSide.NO) == PriceLevel(D("0.60"), D("10"))

    def test_best_no_bid_becomes_the_cheapest_yes_ask(self) -> None:
        """Descending bids map to ascending asks; getting this backwards would
        price every basket off the *worst* available level."""
        book = build({BookSide.NO: {"0.55": "7", "0.50": "3", "0.58": "1"}})
        assert book.ask_levels(BookSide.YES) == [
            PriceLevel(D("0.42"), D("1")),
            PriceLevel(D("0.45"), D("7")),
            PriceLevel(D("0.50"), D("3")),
        ]

    def test_a_side_with_no_opposing_bids_has_no_ask(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        assert book.best_ask(BookSide.YES) is None

    def test_own_side_bids_are_not_asks(self) -> None:
        """The bug this guards: reading a YES bid at $0.40 as a YES ask at
        $0.40 makes a basket look far cheaper than it can be bought for."""
        book = build({BookSide.YES: {"0.40": "10"}, BookSide.NO: {"0.55": "7"}})
        yes_ask = book.best_ask(BookSide.YES)
        assert yes_ask is not None
        assert yes_ask.price_dollars == D("0.45")
        assert yes_ask.price_dollars != D("0.40")

    def test_matches_the_venues_own_quoted_ask(self) -> None:
        """Captured live on 2026-08-12: KXNFLGAME-26AUG15DALSEA-SEA quoted
        yes_bid 0.5900 / yes_ask 0.6000 against a best NO bid of 0.4000."""
        book = build({BookSide.YES: {"0.5900": "152.00"}, BookSide.NO: {"0.4000": "11392.59"}})
        yes_ask = book.best_ask(BookSide.YES)
        no_ask = book.best_ask(BookSide.NO)
        assert yes_ask is not None
        assert no_ask is not None
        assert yes_ask.price_dollars == D("0.6000")
        assert no_ask.price_dollars == D("0.4100")

    @given(
        yes_ticks=st.integers(min_value=1, max_value=9999),
        no_ticks=st.integers(min_value=1, max_value=9999),
    )
    def test_ask_and_bid_are_complements(self, yes_ticks: int, no_ticks: int) -> None:
        yes_price = D(yes_ticks).scaleb(-4)
        no_price = D(no_ticks).scaleb(-4)
        book = build({BookSide.YES: {str(yes_price): "1"}, BookSide.NO: {str(no_price): "1"}})
        yes_ask = book.best_ask(BookSide.YES)
        no_ask = book.best_ask(BookSide.NO)
        assert yes_ask is not None
        assert no_ask is not None
        assert yes_ask.price_dollars == D("1.00") - no_price
        assert no_ask.price_dollars == D("1.00") - yes_price


class TestDeltas:
    def test_delta_adds_size(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("5")), sequence=2)
        assert book.bids(BookSide.YES) == [PriceLevel(D("0.40"), D("15"))]

    def test_delta_removes_size(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("-4")), sequence=2)
        assert book.bids(BookSide.YES) == [PriceLevel(D("0.40"), D("6"))]

    def test_delta_to_zero_removes_the_level(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("-10")), sequence=2)
        assert book.bids(BookSide.YES) == []

    def test_delta_creates_a_new_level(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        book.apply_delta(BookDelta(BookSide.YES, D("0.41"), D("3")), sequence=2)
        assert book.bids(BookSide.YES) == [
            PriceLevel(D("0.41"), D("3")),
            PriceLevel(D("0.40"), D("10")),
        ]

    def test_delta_below_zero_invalidates_the_book(self) -> None:
        """Local state has diverged from the venue. The book cannot be trusted
        to be merely 'a bit off' -- every later delta compounds the error."""
        book = build({BookSide.YES: {"0.40": "10"}})
        with pytest.raises(BookIntegrityError, match="diverged"):
            book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("-11")), sequence=2)
        assert not book.is_complete

    def test_delta_on_incomplete_book_is_refused(self) -> None:
        book = OrderBook("TEST")
        with pytest.raises(BookIntegrityError, match="await a snapshot"):
            book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("1")), sequence=2)

    def test_sequence_gap_invalidates_the_book(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        with pytest.raises(BookIntegrityError, match="sequence gap"):
            book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("1")), sequence=5)
        assert not book.is_complete

    def test_book_stays_unusable_until_a_snapshot(self) -> None:
        book = build({BookSide.YES: {"0.40": "10"}})
        with pytest.raises(BookIntegrityError):
            book.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("1")), sequence=5)

        with pytest.raises(BookIntegrityError, match="incomplete"):
            book.best_ask(BookSide.NO)

        book.apply_snapshot([(BookSide.YES, PriceLevel(D("0.40"), D("2")))], sequence=6)
        assert book.is_complete


class TestChecksum:
    def test_same_levels_produce_the_same_checksum(self) -> None:
        a = build({BookSide.YES: {"0.40": "10"}, BookSide.NO: {"0.55": "7"}}, sequence=1)
        b = build({BookSide.YES: {"0.40": "10"}, BookSide.NO: {"0.55": "7"}}, sequence=99)
        assert a.checksum() == b.checksum()

    def test_trailing_zeros_do_not_change_the_checksum(self) -> None:
        """Decimal keeps trailing zeros, so "0.59" and "0.5900" stringify
        differently while being numerically equal. A REST snapshot (always 4dp)
        and a delta-built book must still compare identical, or replay
        equivalence fails on a formatting artifact rather than a real
        difference."""
        a = build({BookSide.YES: {"0.59": "152"}})
        b = build({BookSide.YES: {"0.5900": "152.00"}})
        assert a.checksum() == b.checksum()

    def test_checksum_ignores_sequence_but_not_state(self) -> None:
        a = build({BookSide.YES: {"0.40": "10"}})
        b = build({BookSide.YES: {"0.40": "8"}})
        b.apply_delta(BookDelta(BookSide.YES, D("0.40"), D("2")), sequence=2)
        assert a.checksum() == b.checksum()

    def test_different_levels_produce_different_checksums(self) -> None:
        a = build({BookSide.YES: {"0.40": "10"}})
        b = build({BookSide.YES: {"0.40": "11"}})
        assert a.checksum() != b.checksum()

    def test_sub_cent_difference_is_visible(self) -> None:
        """A tenth of a cent is a real price difference on a deci_cent market
        and must not be normalised away."""
        a = build({BookSide.YES: {"0.4000": "10"}})
        b = build({BookSide.YES: {"0.4010": "10"}})
        assert a.checksum() != b.checksum()

    def test_side_placement_changes_the_checksum(self) -> None:
        """Same price and size on the wrong side is a different book."""
        a = build({BookSide.YES: {"0.40": "10"}})
        b = build({BookSide.NO: {"0.40": "10"}})
        assert a.checksum() != b.checksum()
