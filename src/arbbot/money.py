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

Venue prices arrive as *decimal dollar strings* with up to four decimal
places, and contract counts as fixed-point strings with up to two. Both are
parsed at the adapter boundary by :func:`parse_venue_dollars` and
:func:`parse_quantity`. The string encoding exists so the values survive
transport exactly; letting a JSON library turn them into floats -- which is
what it does unless told otherwise -- throws away the precision the venue went
to the trouble of preserving.
"""

from __future__ import annotations

import decimal
from decimal import Decimal
from typing import Final

__all__ = [
    "CENT",
    "MAX_PRICE_DOLLARS",
    "MIN_PRICE_DOLLARS",
    "PAYOUT_DOLLARS",
    "PRICE_QUANTUM",
    "QUANTITY_QUANTUM",
    "USD_QUANTUM",
    "ZERO",
    "MoneyError",
    "from_cents",
    "money_context",
    "parse_quantity",
    "parse_venue_dollars",
    "quantize_cost",
    "quantize_proceeds",
    "to_cents_exact",
    "to_usd",
    "validate_price_dollars",
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

#: What one binary contract pays if it settles YES.
PAYOUT_DOLLARS: Final = Decimal("1.00")

#: Finest price granularity the venue quotes. Kalshi expresses prices as dollar
#: strings with up to four decimal places, and tick size varies *per market*:
#: ``linear_cent`` markets step by $0.01, ``deci_cent`` markets by $0.001.
#: Assuming whole cents would silently truncate a real deci-cent quote, which
#: on a basket of several legs is more than enough to invent or destroy an edge.
PRICE_QUANTUM: Final = Decimal("0.0001")

#: Finest size granularity. Contract counts are fixed-point strings with up to
#: two decimals -- fractional positions are real, and a live book routinely
#: shows sizes like ``809.25``. Integer quantities would round every level.
QUANTITY_QUANTUM: Final = Decimal("0.01")

#: An unsettled binary contract trades strictly inside $0 and $1. A quote at
#: either bound is a settled or degenerate market, not a tradeable price, and
#: must be rejected rather than treated as free money.
MIN_PRICE_DOLLARS: Final = PRICE_QUANTUM
MAX_PRICE_DOLLARS: Final = PAYOUT_DOLLARS - PRICE_QUANTUM


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


def _decimal_places(value: Decimal) -> int:
    """Number of digits after the point.

    ``as_tuple().exponent`` is a string sentinel for NaN and infinity, which
    the callers here have already excluded via :func:`to_usd` -- but the type
    checker cannot see that, and a silent ``TypeError`` deep in a parser is a
    worse outcome than an explicit check.
    """
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise MoneyError(f"{value} has no finite exponent")
    return max(0, -exponent)


def parse_venue_dollars(value: str) -> Decimal:
    """Parse a venue ``*_dollars`` price string, e.g. ``"0.5900"``.

    The venue sends prices as decimal strings precisely so they survive
    transport exactly. Parsing them through :class:`float` -- the obvious
    thing a JSON library does by default -- reintroduces the representation
    error the string encoding exists to avoid, which is why this goes via
    :func:`to_usd` and why ``float`` is banned from this module entirely.

    :raises MoneyError: if the value is not an exact decimal, or carries more
        precision than the venue's quoted granularity (which would mean the
        wire format changed and this parser no longer understands it).
    """
    if not isinstance(value, str):
        raise MoneyError(f"venue prices arrive as strings (got {type(value).__name__})")
    amount = to_usd(value)
    if _decimal_places(amount) > _decimal_places(PRICE_QUANTUM):
        raise MoneyError(
            f"price {value!r} is finer than the venue's {PRICE_QUANTUM} granularity; "
            "the wire format may have changed"
        )
    return amount


def parse_quantity(value: str) -> Decimal:
    """Parse a venue ``*_fp`` contract-count string, e.g. ``"809.25"``.

    Counts are fractional. Rounding them to whole contracts would misstate
    available depth at every level, and depth is what decides whether an
    apparent edge is executable at size or is a single contract's worth of
    nothing.
    """
    if not isinstance(value, str):
        raise MoneyError(f"venue quantities arrive as strings (got {type(value).__name__})")
    quantity = to_usd(value)
    if quantity < ZERO:
        raise MoneyError(f"contract count cannot be negative (got {value!r})")
    if _decimal_places(quantity) > _decimal_places(QUANTITY_QUANTUM):
        raise MoneyError(
            f"quantity {value!r} is finer than the venue's {QUANTITY_QUANTUM} granularity; "
            "the wire format may have changed"
        )
    return quantity


def validate_price_dollars(price: Decimal) -> Decimal:
    """Return ``price`` if it is a tradeable binary-contract price.

    :raises MoneyError: if the price is not strictly inside $0 and $1. A leg
        quoted at either bound is not a bargain; it is a market that has
        effectively resolved, and pricing a basket against it would fabricate
        an arbitrage out of a settled contract.
    """
    amount = to_usd(price)
    if not MIN_PRICE_DOLLARS <= amount <= MAX_PRICE_DOLLARS:
        raise MoneyError(
            f"price ${amount} is outside the tradeable range "
            f"${MIN_PRICE_DOLLARS}..${MAX_PRICE_DOLLARS}"
        )
    return amount
