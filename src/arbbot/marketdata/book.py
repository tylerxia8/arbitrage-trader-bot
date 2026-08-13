"""Order-book reconstruction.

The venue sends a snapshot followed by a stream of signed deltas. Applying
them in order reproduces the book; applying them out of order, or missing one,
produces a book that looks plausible and is wrong. There is no way to detect
that from the resulting state alone â€” a level that is 3 contracts too deep
looks exactly like a level that is genuinely that deep.

So this module treats sequence integrity as a precondition rather than a
warning. A gap, a rewind, or a delta that would drive a level negative marks
the book **incomplete**, and an incomplete book refuses to answer questions
about prices until a fresh snapshot repairs it. Rejecting a real opportunity
costs nothing; pricing a basket off a corrupted book costs money.

The other subtlety is what an "ask" is. The venue quotes resting *bids* on
both outcomes of a binary contract. Buying YES means crossing the NO bids: a
NO bid at 55c is an offer to sell YES at 45c. Asks are therefore derived on
demand from the opposite side rather than stored, so the persisted book stays
exactly what the venue reported.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from decimal import Decimal

from arbbot.marketdata.types import BookDelta, BookSide, PriceLevel
from arbbot.money import (
    PAYOUT_DOLLARS,
    PRICE_QUANTUM,
    QUANTITY_QUANTUM,
    ZERO,
    MoneyError,
    validate_price_dollars,
)

__all__ = ["BookIntegrityError", "OrderBook"]


class BookIntegrityError(RuntimeError):
    """Raised when the book is asked to do something its state cannot support.

    Always indicates that reconstruction has diverged from the venue, not that
    the market is unusual.
    """


class OrderBook:
    """Reconstructed book for a single market.

    Not thread-safe; one book belongs to one consumer task.
    """

    __slots__ = ("_complete", "_sequence", "_sides", "ticker")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self._sides: dict[BookSide, dict[Decimal, Decimal]] = {BookSide.YES: {}, BookSide.NO: {}}
        self._sequence: int | None = None
        self._complete = False

    # -- state ----------------------------------------------------------
    @property
    def sequence(self) -> int | None:
        """Sequence number of the last message applied."""
        return self._sequence

    @property
    def is_complete(self) -> bool:
        """Whether the book is a faithful reconstruction.

        ``False`` before the first snapshot and after any integrity failure.
        Evaluations must reject on ``book_incomplete`` rather than reading
        prices from a book in this state.
        """
        return self._complete

    def invalidate(self) -> None:
        """Mark the book unusable until the next snapshot.

        Called on sequence gaps, reconnects, and parse failures. The levels are
        deliberately retained for diagnostics but cannot be read through the
        price accessors while incomplete.
        """
        self._complete = False

    # -- mutation -------------------------------------------------------
    def apply_snapshot(self, levels: Iterable[tuple[BookSide, PriceLevel]], sequence: int) -> None:
        """Replace the entire book.

        A snapshot is the only thing that can repair an incomplete book, so it
        always resets state wholesale rather than merging.
        """
        fresh: dict[BookSide, dict[Decimal, Decimal]] = {BookSide.YES: {}, BookSide.NO: {}}
        for side, level in levels:
            self._validate_price(level.price_dollars)
            if level.quantity > ZERO:
                fresh[side][level.price_dollars] = level.quantity
        self._sides = fresh
        self._sequence = sequence
        self._complete = True

    def apply_delta(self, delta: BookDelta, sequence: int) -> None:
        """Apply one incremental change.

        :raises BookIntegrityError: if the book is incomplete, the sequence is
            not the immediate successor, or the change would drive a level
            negative. Each of those means local state and venue state have
            diverged, and continuing would silently produce a wrong book.
        """
        if not self._complete:
            raise BookIntegrityError(
                f"{self.ticker}: cannot apply delta to an incomplete book; await a snapshot"
            )
        if self._sequence is not None and sequence != self._sequence + 1:
            self.invalidate()
            raise BookIntegrityError(
                f"{self.ticker}: sequence gap, expected {self._sequence + 1} got {sequence}"
            )

        self._validate_price(delta.price_dollars)
        levels = self._sides[delta.side]
        updated = levels.get(delta.price_dollars, ZERO) + delta.delta

        if updated < ZERO:
            self.invalidate()
            raise BookIntegrityError(
                f"{self.ticker}: delta {delta.delta:+} at ${delta.price_dollars} "
                f"({delta.side.value}) would leave {updated} resting; local state has diverged"
            )

        if updated == ZERO:
            levels.pop(delta.price_dollars, None)
        else:
            levels[delta.price_dollars] = updated

        self._sequence = sequence

    # -- reads ----------------------------------------------------------
    def bids(self, side: BookSide) -> list[PriceLevel]:
        """Resting bids on ``side``, best (highest) price first."""
        self._require_complete()
        return [
            PriceLevel(price, qty) for price, qty in sorted(self._sides[side].items(), reverse=True)
        ]

    def ask_levels(self, side: BookSide) -> list[PriceLevel]:
        """Executable prices to *buy* ``side``, cheapest first.

        Derived from the opposite side's resting bids: a NO bid at 55c is an
        offer to sell YES at 45c. The best NO bid therefore becomes the
        cheapest YES ask, so descending bids map to ascending asks.
        """
        self._require_complete()
        opposite = self._sides[side.opposite]
        return [
            PriceLevel(PAYOUT_DOLLARS - price, qty)
            for price, qty in sorted(opposite.items(), reverse=True)
        ]

    def best_ask(self, side: BookSide) -> PriceLevel | None:
        """Cheapest executable price to buy ``side``, or ``None`` if no offer."""
        levels = self.ask_levels(side)
        return levels[0] if levels else None

    def total_quantity(self, side: BookSide) -> Decimal:
        """Total resting size on ``side``. Available on an incomplete book,
        because operators need it for diagnostics."""
        return sum(self._sides[side].values(), ZERO)

    def checksum(self) -> str:
        """Deterministic fingerprint of the book's levels.

        Replay compares checksums rather than objects: two books built by
        different paths (live application versus archive replay) must be
        byte-identical in state, and a checksum makes that comparison exact
        and cheap to store alongside a snapshot.

        Excludes the sequence number so that the same levels reached by
        different routes compare equal.

        Values are quantised before hashing because ``Decimal`` keeps its
        trailing zeros: ``Decimal("0.59")`` and ``Decimal("0.5900")`` are
        numerically equal and hash equal as dict keys, but stringify
        differently. Without normalisation a book assembled from a REST
        snapshot (four decimal places) and the same book assembled from
        deltas (however many the venue happened to send) would produce
        different checksums for identical state -- and the replay-equivalence
        guarantee would fail on a formatting artifact.
        """
        digest = hashlib.sha256()
        for side in (BookSide.YES, BookSide.NO):
            digest.update(side.value.encode())
            for price, qty in sorted(self._sides[side].items()):
                normalized_price = price.quantize(PRICE_QUANTUM)
                normalized_qty = qty.quantize(QUANTITY_QUANTUM)
                digest.update(f"|{normalized_price}:{normalized_qty}".encode())
            digest.update(b";")
        return digest.hexdigest()

    def levels_by_side(self) -> Mapping[BookSide, Mapping[Decimal, Decimal]]:
        """Raw levels, for persistence and diagnostics."""
        return {side: dict(levels) for side, levels in self._sides.items()}

    # -- internals ------------------------------------------------------
    def _require_complete(self) -> None:
        if not self._complete:
            raise BookIntegrityError(
                f"{self.ticker}: book is incomplete; no price may be read from it"
            )

    @staticmethod
    def _validate_price(price_dollars: Decimal) -> None:
        """Reject prices outside the open interval ($0, $1).

        A resting bid at either bound is not a price on an unsettled binary
        contract, and admitting one would let a basket be priced against a
        market that has effectively resolved.
        """
        try:
            validate_price_dollars(price_dollars)
        except MoneyError as exc:
            raise BookIntegrityError(str(exc)) from exc

    def __repr__(self) -> str:
        state = "complete" if self._complete else "INCOMPLETE"
        return (
            f"<OrderBook {self.ticker} seq={self._sequence} {state} "
            f"yes={len(self._sides[BookSide.YES])} no={len(self._sides[BookSide.NO])}>"
        )
