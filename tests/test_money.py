"""Money primitives.

The rounding-direction tests are the important ones. Everything downstream --
fee deduction, depth-weighted cost, net edge -- inherits its safety from the
guarantee that a cost is never understated and a payout never overstated.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arbbot.money import (
    CENT,
    MoneyError,
    from_cents,
    quantize_cost,
    quantize_proceeds,
    to_cents_exact,
    to_usd,
    validate_price_cents,
)


class TestToUsd:
    def test_rejects_float(self) -> None:
        with pytest.raises(MoneyError, match="float is not permitted"):
            to_usd(0.35)  # type: ignore[arg-type]

    def test_rejects_float_that_looks_exact(self) -> None:
        """0.35 is not 0.35, which is exactly why the type is refused."""
        with pytest.raises(MoneyError):
            to_usd(float(35) / 100)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(5, Decimal("5")), ("0.35", Decimal("0.35")), (Decimal("1.23"), Decimal("1.23"))],
    )
    def test_accepts_exact_sources(self, value: int | str | Decimal, expected: Decimal) -> None:
        assert to_usd(value) == expected

    def test_rejects_nan_and_infinity(self) -> None:
        for bad in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(MoneyError, match="finite"):
                to_usd(bad)

    def test_rejects_unparseable(self) -> None:
        with pytest.raises(MoneyError, match="cannot represent"):
            to_usd("thirty-five cents")


class TestVenueConversion:
    def test_from_cents(self) -> None:
        assert from_cents(35) == Decimal("0.35")
        assert from_cents(1) == Decimal("0.01")

    def test_from_cents_rejects_bool(self) -> None:
        """bool is an int subclass; True would silently price a leg at 1c."""
        with pytest.raises(MoneyError, match="plain integers"):
            from_cents(True)

    def test_to_cents_exact_roundtrips(self) -> None:
        assert to_cents_exact(Decimal("0.35")) == 35

    def test_to_cents_exact_refuses_a_residue(self) -> None:
        with pytest.raises(MoneyError, match="not a whole number of cents"):
            to_cents_exact(Decimal("0.355"))


class TestConservativeRounding:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            ("0.3501", "0.36"),
            ("0.3500", "0.35"),
            ("0.3599", "0.36"),
            ("1.000001", "1.01"),
        ],
    )
    def test_cost_rounds_up(self, amount: str, expected: str) -> None:
        assert quantize_cost(Decimal(amount)) == Decimal(expected)

    @pytest.mark.parametrize(
        ("amount", "expected"),
        [
            ("0.3599", "0.35"),
            ("0.3500", "0.35"),
            ("0.3501", "0.35"),
            ("0.999999", "0.99"),
        ],
    )
    def test_proceeds_round_down(self, amount: str, expected: str) -> None:
        assert quantize_proceeds(Decimal(amount)) == Decimal(expected)

    def test_a_sub_cent_edge_cannot_survive_rounding(self) -> None:
        """A basket clearing by a third of a cent must not round into profit.

        Payout 1.00, cost 0.9967. The true edge is +0.0033. After conservative
        rounding the recorded edge is 1.00 - 1.00 = 0, so the candidate fails
        the minimum-edge test rather than being accepted on a rounding artifact.
        """
        payout = quantize_proceeds(Decimal("1.0000"))
        cost = quantize_cost(Decimal("0.9967"))
        assert payout - cost == Decimal("0.00")


@given(
    st.decimals(
        min_value=Decimal("-1000"),
        max_value=Decimal("1000"),
        places=8,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_rounding_brackets_the_true_value(amount: Decimal) -> None:
    """Proceeds <= true value <= cost, and neither strays a full cent."""
    low = quantize_proceeds(amount)
    high = quantize_cost(amount)

    assert low <= amount <= high
    assert amount - low < CENT
    assert high - amount < CENT


class TestPriceValidation:
    @pytest.mark.parametrize("price", [1, 50, 99])
    def test_accepts_tradeable_prices(self, price: int) -> None:
        assert validate_price_cents(price) == price

    @pytest.mark.parametrize("price", [0, 100, -1, 101])
    def test_rejects_untradeable_prices(self, price: int) -> None:
        """A leg at 0c or 100c has effectively resolved; pricing a basket
        against it would fabricate an arbitrage out of a settled market."""
        with pytest.raises(MoneyError, match="outside the tradeable range"):
            validate_price_cents(price)

    def test_rejects_bool(self) -> None:
        with pytest.raises(MoneyError, match="plain integers"):
            validate_price_cents(True)
