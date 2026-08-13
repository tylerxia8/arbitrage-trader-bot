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
from decimal import Decimal

from arbbot.money import PAYOUT_DOLLARS, ZERO

__all__ = [
    "PAYOUT_DOLLARS",
    "BookDelta",
    "BookEvent",
    "BookSide",
    "DeltaEvent",
    "MarketSnapshotRecord",
    "MarketStatus",
    "PriceLevel",
    "SnapshotEvent",
]

# The complement of a YES bid at price p is a NO ask at ($1.00 - p), which is
# the identity the whole order-book model rests on. PAYOUT_DOLLARS is
# re-exported from arbbot.money so there is exactly one definition of what a
# contract pays.


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
    """A resting quantity at a price.

    Both fields are exact decimals -- never floats, and never integers. Prices
    carry up to four decimal places because tick size varies per market: a
    ``deci_cent`` market quotes in $0.001 steps. Contract counts carry two,
    because fractional positions are real; a live book shows sizes like
    ``809.25``. Truncating either to whole units misstates the cost or the
    depth of every level it touches.
    """

    price_dollars: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.quantity < ZERO:
            raise ValueError(f"negative quantity at ${self.price_dollars}: {self.quantity}")


@dataclass(frozen=True, slots=True)
class BookDelta:
    """An incremental change to one price level.

    ``delta`` is a signed contract count: positive adds resting size, negative
    removes it. The venue sends changes rather than absolute levels, so a
    dropped message corrupts every subsequent level — which is why sequence
    gaps invalidate the book rather than merely warning.
    """

    side: BookSide
    price_dollars: Decimal
    delta: Decimal


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
