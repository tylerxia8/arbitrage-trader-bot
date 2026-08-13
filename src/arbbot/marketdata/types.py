"""Normalized market-data vocabulary.

Venue payloads are parsed into these types at the adapter boundary. Everything
downstream — book reconstruction, gap detection, replay, detection — speaks
only this vocabulary, so a venue changing its wire format is a change to one
parser rather than a change to the system.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BINARY_PAYOUT_CENTS",
    "BookDelta",
    "BookEvent",
    "BookSide",
    "DeltaEvent",
    "MarketSnapshotRecord",
    "MarketStatus",
    "PriceLevel",
    "SnapshotEvent",
]

#: A binary event contract settles at 100 cents or 0. The complement of a YES
#: bid at price p is a NO ask at (100 - p), which is the identity the whole
#: order-book model rests on.
BINARY_PAYOUT_CENTS: Final = 100


class BookSide(enum.StrEnum):
    """Which outcome a resting bid is for.

    Kalshi's book quotes *resting bids on both outcomes* rather than a
    conventional bid/ask ladder. A YES bid at 42c and a NO bid at 55c mean the
    best price to buy YES is 45c (100 - 55) and the best price to buy NO is
    58c (100 - 42). Keeping the raw sides here, and deriving asks on demand,
    means the stored book is exactly what the venue said.
    """

    YES = "yes"
    NO = "no"

    @property
    def opposite(self) -> BookSide:
        return BookSide.NO if self is BookSide.YES else BookSide.YES


class MarketStatus(enum.StrEnum):
    """Normalized market lifecycle state."""

    UNOPENED = "unopened"
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"
    """The venue reported a status this build does not recognise. Treated as
    not-tradeable: an unrecognised status is not evidence that trading is safe."""

    @property
    def is_tradeable(self) -> bool:
        return self is MarketStatus.OPEN


@dataclass(frozen=True, slots=True)
class PriceLevel:
    """A resting quantity at a price. Prices are integer cents; sizes are
    whole contracts. Neither is ever a float."""

    price_cents: int
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError(f"negative quantity at {self.price_cents}c: {self.quantity}")


@dataclass(frozen=True, slots=True)
class BookDelta:
    """An incremental change to one price level.

    ``delta`` is signed: positive adds resting size, negative removes it. The
    venue sends changes rather than absolute levels, so a dropped message
    corrupts every subsequent level — which is why sequence gaps invalidate
    the book rather than merely warning.
    """

    side: BookSide
    price_cents: int
    delta: int


@dataclass(frozen=True, slots=True)
class SnapshotEvent:
    """A full book replacement for one market."""

    ticker: str
    sequence: int
    levels: tuple[tuple[BookSide, PriceLevel], ...]


@dataclass(frozen=True, slots=True)
class DeltaEvent:
    """An incremental change to one market's book."""

    ticker: str
    sequence: int
    delta: BookDelta


#: Anything that can move a book forward. Adapters decode venue payloads into
#: these, and reconstruction consumes only these -- which is what lets live
#: collection and archive replay share one code path.
BookEvent = SnapshotEvent | DeltaEvent


@dataclass(frozen=True, slots=True)
class MarketSnapshotRecord:
    """Normalized market metadata at a point in time."""

    venue: str
    ticker: str
    event_id: str
    title: str
    status: MarketStatus
    close_ts: dt.datetime | None
    settlement_ts: dt.datetime | None
