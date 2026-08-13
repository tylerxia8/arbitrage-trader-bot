"""Venue fees.

The golden cases are the venue's own published fee table, transcribed from the
CFTC filing. Reproducing a table someone else computed is the only way to know
the formula was read correctly, and the go-live checklist requires exactly
this: "exact fee model verified against venue examples".

:class:`TestPublishedRanges` is the confirmation itself. The venue's current
schedule quotes a fee *range* per series rather than a coefficient, and all
four endpoints -- taker and maker, at a penny and at fifty cents -- fall out of
this formula to the cent. That triangulates the rate, the shape and the
rounding at once, which a single quoted number could not.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from arbbot.fees.schedule import (
    BASE_MAKER_RATE,
    BASE_TAKER_RATE,
    GENERAL_TRADING_FEE,
    INDEX_TRADING_FEE,
    KALSHI_SCHEDULE,
    FeeRule,
    FeeSchedule,
    Liquidity,
    UnknownFeeError,
    UnverifiedFeeError,
)
from arbbot.money import ZERO

D = Decimal
TICKER = "KXHIGHTATL-26AUG13-T92"


class TestPublishedTable:
    """Every row of the venue's General Trading Fees table."""

    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0.01", "0.01"),
            ("0.05", "0.01"),
            ("0.10", "0.01"),
            ("0.15", "0.01"),
            ("0.20", "0.02"),
            ("0.25", "0.02"),
            ("0.50", "0.02"),
            ("0.80", "0.02"),
            ("0.85", "0.01"),
            ("0.95", "0.01"),
            ("0.99", "0.01"),
        ],
    )
    def test_fee_for_one_contract(self, price: str, expected: str) -> None:
        assert GENERAL_TRADING_FEE.fee(D(price), D("1")) == D(expected)

    @pytest.mark.parametrize(
        ("price", "expected"),
        [
            ("0.01", "0.07"),
            ("0.05", "0.34"),
            ("0.10", "0.63"),
            ("0.15", "0.90"),
            ("0.20", "1.12"),
            ("0.25", "1.32"),
            ("0.30", "1.47"),
            ("0.35", "1.60"),
            ("0.40", "1.68"),
            ("0.45", "1.74"),
            ("0.50", "1.75"),
            ("0.55", "1.74"),
            ("0.60", "1.68"),
            ("0.65", "1.60"),
            ("0.70", "1.47"),
            ("0.75", "1.32"),
            ("0.80", "1.12"),
            ("0.85", "0.90"),
            ("0.90", "0.63"),
            ("0.95", "0.34"),
            ("0.99", "0.07"),
        ],
    )
    def test_fee_for_one_hundred_contracts(self, price: str, expected: str) -> None:
        assert GENERAL_TRADING_FEE.fee(D(price), D("100")) == D(expected)

    def test_the_fee_peaks_at_a_fifty_cent_contract(self) -> None:
        """P x (1-P) is maximised at 0.50, and the table agrees: $1.75 per 100."""
        peak = GENERAL_TRADING_FEE.fee(D("0.50"), D("100"))
        assert peak == D("1.75")
        assert GENERAL_TRADING_FEE.fee(D("0.30"), D("100")) < peak
        assert GENERAL_TRADING_FEE.fee(D("0.70"), D("100")) < peak


class TestRounding:
    def test_the_fee_rounds_up_to_the_cent(self) -> None:
        """The venue's rule, and the conservative direction: a fee understated
        by a fraction of a cent is edge invented out of arithmetic."""
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175, which is not a whole cent.
        assert GENERAL_TRADING_FEE.fee(D("0.50"), D("1")) == D("0.02")

    def test_a_tiny_trade_still_costs_a_penny(self) -> None:
        """The floor that kills small baskets: 0.07 x 1 x 0.01 x 0.99 is
        two thousandths of a cent, and it is still charged as a full cent."""
        assert GENERAL_TRADING_FEE.fee(D("0.01"), D("1")) == D("0.01")

    def test_fractional_contracts_are_priced(self) -> None:
        """Contract counts are fractional on this venue."""
        assert GENERAL_TRADING_FEE.fee(D("0.50"), D("4.50")) > 0

    def test_zero_contracts_cost_nothing(self) -> None:
        assert GENERAL_TRADING_FEE.fee(D("0.50"), D("0")) == D("0")


class TestBaskets:
    def test_fees_are_summed_per_leg(self) -> None:
        """Rounding is per trade. Pricing a basket as one notional trade would
        understate it by most of its cost at small size."""
        legs = [(f"LEG{i}", D("0.01")) for i in range(6)]
        assert KALSHI_SCHEDULE.basket_fee(legs, D("1")) == D("0.06")

    def test_a_six_leg_basket_has_a_six_cent_floor(self) -> None:
        """The structural fact that decides most of this. A basket whose gross
        edge is under six cents cannot survive at one contract, no matter how
        attractive the price looks."""
        legs = [(f"LEG{i}", D("0.01")) for i in range(6)]
        floor = KALSHI_SCHEDULE.basket_fee(legs, D("1"))
        assert floor == D("0.06")

    def test_the_floor_amortises_with_size(self) -> None:
        """Ten times the size is nowhere near ten times the fee at a penny."""
        legs = [(f"LEG{i}", D("0.01")) for i in range(6)]
        one = KALSHI_SCHEDULE.basket_fee(legs, D("1"))
        ten = KALSHI_SCHEDULE.basket_fee(legs, D("10"))
        assert ten < one * 10

    def test_a_real_basket_from_the_archive(self) -> None:
        """The Philadelphia set: five legs at a penny and one at $0.79, at the
        four contracts the thinnest leg allowed."""
        legs = [
            ("PHIL-A", D("0.01")),
            ("PHIL-B", D("0.01")),
            ("PHIL-C", D("0.79")),
            ("PHIL-D", D("0.01")),
            ("PHIL-E", D("0.01")),
            ("PHIL-F", D("0.01")),
        ]
        fee = KALSHI_SCHEDULE.basket_fee(legs, D("4"))
        # Five pennies plus ceil(0.07 * 4 * 0.79 * 0.21) = 0.05
        assert fee == D("0.10")


class TestUnknownFees:
    def test_a_ticker_with_no_rule_raises(self) -> None:
        """FR-010. Defaulting to zero would make every unpriceable market look
        like the most profitable one on the board."""
        empty = FeeSchedule(())
        with pytest.raises(UnknownFeeError, match="unknown_fee"):
            empty.trade_fee(TICKER, D("0.50"), D("1"))

    def test_a_rule_not_yet_in_force_does_not_apply(self) -> None:
        future = FeeRule(
            name="future",
            multiplier=D("1"),
            maker_multiplier=ZERO,
            source="test",
            effective_from=dt.date(2030, 1, 1),
            verified=True,
        )
        with pytest.raises(UnknownFeeError):
            FeeSchedule((future,)).trade_fee(TICKER, D("0.50"), D("1"), on=dt.date(2026, 8, 13))

    def test_an_unestablished_maker_fee_raises_rather_than_being_free(self) -> None:
        """The FR-010 trap in its most tempting form. A maker fee nobody has
        looked up is not zero, and defaulting it to zero would make resting
        orders look costless on exactly the series most likely to charge for
        them."""
        with pytest.raises(UnknownFeeError, match="unknown_fee"):
            INDEX_TRADING_FEE.fee(D("0.50"), D("100"), liquidity=Liquidity.MAKER)


class TestPublishedRanges:
    """The venue's current published schedule quotes a fee *range* per series
    for a hundred contracts. Reproducing all four endpoints confirms the
    coefficient, the P x (1-P) shape and the round-up rule together -- which no
    single quoted rate could, and which is why this is the verification."""

    def test_the_standard_taker_range(self) -> None:
        assert GENERAL_TRADING_FEE.fee(D("0.01"), D("100")) == D("0.07")
        assert GENERAL_TRADING_FEE.fee(D("0.50"), D("100")) == D("1.75")

    def test_the_maker_range_on_a_series_that_charges_one(self) -> None:
        maker = FeeRule(
            name="test-maker",
            multiplier=D("1"),
            maker_multiplier=D("1"),
            source="Kalshi published fee schedule, non-standard series rows",
            effective_from=dt.date(2022, 9, 12),
            verified=True,
        )
        assert maker.fee(D("0.01"), D("100"), liquidity=Liquidity.MAKER) == D("0.02")
        assert maker.fee(D("0.50"), D("100"), liquidity=Liquidity.MAKER) == D("0.44")

    def test_making_is_a_quarter_the_cost_of_taking(self) -> None:
        assert BASE_MAKER_RATE == BASE_TAKER_RATE / 4


class TestLiquidity:
    def test_a_basket_is_priced_as_a_taker_by_default(self) -> None:
        """Assembling a basket means crossing the spread on every leg. A
        resting order is not an arbitrage until it fills, so pricing the
        default as a maker would zero out the entire fee model."""
        legs = [(f"LEG{i}", D("0.50")) for i in range(6)]
        assert KALSHI_SCHEDULE.basket_fee(legs, D("100")) == D("10.50")

    def test_resting_orders_are_free_on_a_standard_series(self) -> None:
        """A fact about the strategy space, not only about arithmetic: the
        entire fee burden this system models is the price of immediacy."""
        assert (
            KALSHI_SCHEDULE.trade_fee(TICKER, D("0.50"), D("100"), liquidity=Liquidity.MAKER)
            == ZERO
        )


class TestVerification:
    def test_the_general_rule_is_verified(self) -> None:
        """Confirmed on 2026-08-13 against the venue's published schedule,
        whose hundred-contract range this formula reproduces exactly."""
        assert GENERAL_TRADING_FEE.verified is True

    def test_qualification_now_prices_the_general_rule(self) -> None:
        assert KALSHI_SCHEDULE.trade_fee(TICKER, D("0.50"), D("1"), require_verified=True) == D(
            "0.02"
        )

    def test_qualification_still_refuses_the_unconfirmed_override(self) -> None:
        """One rule being confirmed does not confirm the rest. The index
        multiplier rests on the 2022 filing alone and was not among the
        non-standard series read off the published schedule."""
        assert INDEX_TRADING_FEE.verified is False
        with pytest.raises(UnverifiedFeeError, match="confirm it against"):
            KALSHI_SCHEDULE.trade_fee("INXD-1", D("0.50"), D("1"), require_verified=True)

    def test_every_rule_cites_a_source(self) -> None:
        for rule in (GENERAL_TRADING_FEE, INDEX_TRADING_FEE):
            assert rule.source
            assert rule.effective_from


class TestOverrides:
    def test_index_markets_take_the_lower_rate(self) -> None:
        general = KALSHI_SCHEDULE.trade_fee("KXHIGHTATL-1", D("0.50"), D("100"))
        index = KALSHI_SCHEDULE.trade_fee("INXD-1", D("0.50"), D("100"))
        assert index < general

    def test_the_override_matches_the_published_index_table(self) -> None:
        """Multiplier 0.5 on the base rate: 0.035 x 100 x 0.5 x 0.5 = 0.875,
        and the filing's index table agrees at half the general rate."""
        assert KALSHI_SCHEDULE.trade_fee("INXD-1", D("0.50"), D("100")) == D("0.88")
