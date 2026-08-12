"""Fixed-point money primitives.

Every price, quantity, fee, and P&L figure in this system is exact. Binary
floating point is prohibited in the money path (FR-002) because a repeated
0.01 rounding error is indistinguishable from the edge this system is trying
to measure -- a basket that clears by a third of a cent is exactly the case
that must not be corrupted by representation error.

Two rules follow from that, and both are enforced here rather than left to
convention:

1.  **No float ever enters.** :func:`to_usd` rejects ``float`` at runtime, and
    ``tests/test_no_float_in_money_path.py`` rejects it statically at the AST
    level across every module under the money path. Constructing
    ``Decimal(0.1)`` yields 0.1000000000000000055511151231257827, so silently
    accepting a float would defeat the entire point of using Decimal.

2.  **Rounding always favours rejection.** Costs round up, proceeds round
    down. A candidate must never look profitable because of a rounding
    direction. See :func:`quantize_cost` and :func:`quantize_proceeds`.

Venue prices arrive as integer cents (1..99 for a binary contract). Adapters
convert at the boundary with :func:`from_cents`; nothing downstream should
handle a raw venue integer as if it were dollars.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Final

__all__ = [
    "CENT",
    "MAX_PRICE_CENTS",
    "MIN_PRICE_CENTS",
    "USD_QUANTUM",
    "ZERO",
    "MoneyError",
    "from_cents",
    "money_context",
    "quantize_cost",
    "quantize_proceeds",
    "to_cents_exact",
    "to_usd",
    "validate_price_cents",
]


class MoneyError(ValueError):
    """Raised when a value cannot be represented exactly as money."""


#: Smallest unit the venue quotes and settles in.
CENT: Final = Decimal("0.01")

#: Working precision for intermediate arithmetic (fee rates, depth-weighted
#: averages). Results are quantised back to :data:`CENT` at the boundary --
#: this quantum exists so that a chain of intermediate operations does not
#: accumulate its own rounding error before the conservative rounding applies.
USD_QUANTUM: Final = Decimal("0.00000001")

ZERO: Final = Decimal("0")

#: A binary contract that has not settled trades strictly inside 0 and 100
#: cents. A quote at 0 or 100 is a settled or degenerate market, not a
#: tradeable price, and must be rejected rather than treated as free money.
MIN_PRICE_CENTS: Final = 1
MAX_PRICE_CENTS: Final = 99


def money_context() -> decimal.Context:
    """Return the decimal context the money path runs under.

    Precision is generous because intermediate products (price x quantity x
    fee rate) are cheap, and the traps make silent corruption impossible:
    an overflow or an undefined operation raises instead of quietly
    propagating a NaN into a trading decision.
    """
    return decimal.Context(
        prec=34,
        rounding=decimal.ROUND_HALF_UP,
        traps=[
            decimal.InvalidOperation,
            decimal.DivisionByZero,
            decimal.Overflow,
        ],
    )


def to_usd(value: int | str | Decimal) -> Decimal:
    """Convert an exact value to a USD :class:`~decimal.Decimal`.

    ``float`` is rejected rather than converted. There is no correct way to
    interpret a float that has already lost precision, so the only safe
    action is to refuse it and force the caller to supply an exact source.

    :raises MoneyError: if the value is a float, or is not exactly representable.
    """
    # Naming `float` here is a rejection, not a use; the FR-002 checker exempts
    # isinstance guards precisely so this pattern stays available.
    if isinstance(value, float):
        raise MoneyError(
            f"float is not permitted in the money path (got {value!r}); "
            "pass an int, a str, or a Decimal from an exact source"
        )
    if isinstance(value, Decimal):
        candidate = value
    else:
        try:
            candidate = Decimal(value)
        except decimal.InvalidOperation as exc:
            raise MoneyError(f"cannot represent {value!r} as money") from exc

    if not candidate.is_finite():
        raise MoneyError(f"money must be finite (got {candidate})")
    return candidate


def from_cents(price_cents: int) -> Decimal:
    """Convert an integer venue price in cents to exact USD."""
    if not isinstance(price_cents, int) or isinstance(price_cents, bool):
        raise MoneyError(f"venue prices must be plain integers (got {price_cents!r})")
    return Decimal(price_cents) * CENT


def to_cents_exact(amount: Decimal) -> int:
    """Convert exact USD back to integer cents.

    :raises MoneyError: if the amount is not a whole number of cents. Callers
        that expect a residue must quantise first, deliberately choosing a
        direction via :func:`quantize_cost` or :func:`quantize_proceeds`.
    """
    scaled = to_usd(amount) / CENT
    if scaled != scaled.to_integral_value():
        raise MoneyError(f"{amount} is not a whole number of cents")
    return int(scaled)


def quantize_cost(amount: Decimal) -> Decimal:
    """Round a cost or fee **up** to the next cent.

    Always overstates what is paid. Applied to acquisition costs, fees,
    slippage reserves, and capital charges so that a rounding residue can
    never manufacture edge that does not exist.
    """
    with decimal.localcontext(money_context()):
        return to_usd(amount).quantize(CENT, rounding=decimal.ROUND_CEILING)


def quantize_proceeds(amount: Decimal) -> Decimal:
    """Round proceeds or a guaranteed payout **down** to the cent.

    Always understates what is received. The mirror of :func:`quantize_cost`:
    together they guarantee that a computed net edge is a lower bound on the
    true edge, never an upper one.
    """
    with decimal.localcontext(money_context()):
        return to_usd(amount).quantize(CENT, rounding=decimal.ROUND_FLOOR)


def validate_price_cents(price_cents: int) -> int:
    """Return ``price_cents`` if it is a tradeable binary-contract price.

    :raises MoneyError: if the price is outside 1..99. A leg quoted at 0 or
        100 is not a bargain; it is a market that has effectively resolved,
        and pricing a basket against it would fabricate an arbitrage.
    """
    if not isinstance(price_cents, int) or isinstance(price_cents, bool):
        raise MoneyError(f"venue prices must be plain integers (got {price_cents!r})")
    if not MIN_PRICE_CENTS <= price_cents <= MAX_PRICE_CENTS:
        raise MoneyError(
            f"price {price_cents}c is outside the tradeable range "
            f"{MIN_PRICE_CENTS}..{MAX_PRICE_CENTS}"
        )
    return price_cents
