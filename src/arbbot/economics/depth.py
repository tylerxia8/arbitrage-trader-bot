"""Executable cost across order-book depth (FR-009).

Top-of-book is a price for one trade, not for the trade you want. Everything
this system has reported so far used the best ask alone, which is right only
when the quantity fits inside it -- and the observed books put four contracts
at the top of a leg and nothing behind it for cents.

So cost is computed by walking levels: take what is available at the best
price, then the next, until the requested quantity is met. Two rules follow,
and both are refusals rather than adjustments:

**Insufficient depth rejects.** If the book cannot fill the quantity, this
returns a partial walk and the caller must reject with ``insufficient_depth``.
Filling what is available and calling it a basket is how a multi-leg trade
turns into a directional position.

**No extrapolation past the last level.** A book that ends is a book that ends.
Assuming the next level exists at a plausible price is inventing liquidity,
and the invented part is exactly the part that does not fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arbbot.marketdata.types import PriceLevel
from arbbot.money import ZERO, quantize_cost

__all__ = ["DepthWalk", "walk_levels"]


@dataclass(frozen=True, slots=True)
class DepthWalk:
    """What it costs to buy a quantity, level by level."""

    requested: Decimal
    filled: Decimal
    cost: Decimal
    levels_used: int
    worst_price: Decimal | None
    """Price of the deepest level touched -- what the last contract cost."""

    @property
    def is_complete(self) -> bool:
        """Whether the book could supply the whole quantity."""
        return self.filled >= self.requested

    @property
    def shortfall(self) -> Decimal:
        return max(self.requested - self.filled, ZERO)

    @property
    def average_price(self) -> Decimal | None:
        """Mean price paid. ``None`` when nothing filled.

        Reported for diagnostics only. Decisions use :attr:`cost`, because an
        average silently hides that the last contract cost far more than the
        first.
        """
        if self.filled <= ZERO:
            return None
        return self.cost / self.filled


def walk_levels(levels: list[PriceLevel], quantity: Decimal) -> DepthWalk:
    """Walk ascending ask levels to buy ``quantity``.

    ``levels`` must be cheapest-first, as
    :meth:`~arbbot.marketdata.book.OrderBook.ask_levels` returns them. The
    total is quantised **up** to the cent: a fraction of a cent that rounds in
    our favour is edge manufactured by arithmetic.
    """
    if quantity <= ZERO:
        return DepthWalk(quantity, ZERO, ZERO, 0, None)

    remaining = quantity
    cost = ZERO
    used = 0
    worst: Decimal | None = None

    for level in levels:
        if remaining <= ZERO:
            break
        take = min(remaining, level.quantity)
        if take <= ZERO:
            continue
        cost += take * level.price_dollars
        remaining -= take
        worst = level.price_dollars
        used += 1

    filled = quantity - remaining
    return DepthWalk(
        requested=quantity,
        filled=filled,
        cost=quantize_cost(cost),
        levels_used=used,
        worst_price=worst,
    )
