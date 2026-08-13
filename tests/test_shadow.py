"""Shadow execution (FR-012).

The detector prices a basket as though every leg could be bought at once.
Nothing can, and the gap between those two facts is the entire risk of
multi-leg arbitrage. These tests are mostly about what happens when the
basket does *not* complete, because that is the case the gross figures never
show and the one that turns an arbitrage into a directional bet.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from arbbot.marketdata.types import PriceLevel
from arbbot.shadow import FillOutcome, ShadowConfig, simulate_basket

D = Decimal

#: No vanishing, so fills depend only on depth. Used wherever a test is about
#: something other than the market moving.
CERTAIN = ShadowConfig(level_vanish_probability=D("0"))


def leg(ticker: str, *levels: tuple[str, str]) -> tuple[str, list[PriceLevel]]:
    return ticker, [PriceLevel(D(price), D(size)) for price, size in levels]


class TestCompleteFills:
    def test_a_deep_basket_fills(self) -> None:
        result = simulate_basket(
            [leg("A", ("0.30", "100")), leg("B", ("0.30", "100"))],
            D("10"),
            config=CERTAIN,
        )
        assert result.complete
        assert not result.exposed
        assert result.acquisition_cost == D("6.00")

    def test_latency_accumulates_across_legs(self) -> None:
        """Legs are sequential, so a six-leg basket waits six times before the
        last leg is even attempted."""
        legs = [leg(f"L{i}", ("0.10", "100")) for i in range(6)]
        result = simulate_basket(legs, D("1"), config=CERTAIN)
        assert result.elapsed == dt.timedelta(milliseconds=1500)


class TestIncompleteFills:
    def test_a_thin_leg_leaves_the_basket_exposed(self) -> None:
        """The failure that matters: not a smaller arbitrage, a directional
        position nobody chose."""
        result = simulate_basket(
            [leg("A", ("0.30", "100")), leg("B", ("0.30", "4"))],
            D("10"),
            config=CERTAIN,
        )
        assert not result.complete
        assert result.exposed
        assert result.fills[1].outcome is FillOutcome.PARTIAL

    def test_an_incomplete_basket_pays_an_unwind_cost(self) -> None:
        """Everything acquired has to come back off at a worse price. This is
        the cost the gross figures never show."""
        result = simulate_basket(
            [leg("A", ("0.50", "100")), leg("B", ("0.50", "0.5"))],
            D("10"),
            config=CERTAIN,
        )
        assert result.unwind_loss > 0

    def test_an_empty_leg_is_missed_entirely(self) -> None:
        result = simulate_basket([leg("A", ("0.30", "100")), ("B", [])], D("10"), config=CERTAIN)
        assert result.fills[1].outcome is FillOutcome.MISSED
        assert not result.complete

    def test_a_basket_that_fills_nothing_is_not_exposed(self) -> None:
        """No capital committed means no position to unwind."""
        result = simulate_basket([("A", []), ("B", [])], D("10"), config=CERTAIN)
        assert not result.complete
        assert not result.exposed


class TestVanishingLevels:
    def test_a_certain_vanish_drops_the_best_level(self) -> None:
        """What remains is what someone slower actually gets."""
        always = ShadowConfig(level_vanish_probability=D("1"))
        result = simulate_basket([leg("A", ("0.30", "10"), ("0.40", "10"))], D("10"), config=always)
        assert result.acquisition_cost == D("4.00")

    def test_vanishing_can_make_a_basket_incomplete(self) -> None:
        """The top level was the only depth; when it goes, so does the trade."""
        always = ShadowConfig(level_vanish_probability=D("1"))
        result = simulate_basket([leg("A", ("0.30", "10"))], D("10"), config=always)
        assert not result.complete
        assert result.fills[0].outcome is FillOutcome.MISSED


class TestDeterminism:
    def test_the_same_seed_gives_the_same_result(self) -> None:
        """A falsification report built on an irreproducible simulation is an
        anecdote."""
        legs = [leg(f"L{i}", ("0.15", "10"), ("0.20", "10")) for i in range(6)]

        def run(seed: int) -> tuple[bool, Decimal]:
            result = simulate_basket(legs, D("10"), config=ShadowConfig(seed=seed))
            return result.complete, result.acquisition_cost

        assert run(42) == run(42)

    def test_different_seeds_can_differ(self) -> None:
        legs = [leg(f"L{i}", ("0.15", "10"), ("0.20", "10")) for i in range(8)]
        outcomes = {
            simulate_basket(legs, D("10"), config=ShadowConfig(seed=s)).acquisition_cost
            for s in range(12)
        }
        assert len(outcomes) > 1, "a vanish probability that never fires is not modelling anything"

    def test_a_harsher_assumption_is_never_better(self) -> None:
        """Every assumption in this model makes the outcome worse, so raising
        the vanish probability must not improve the fill rate."""
        legs = [leg(f"L{i}", ("0.15", "10"), ("0.20", "10")) for i in range(6)]
        gentle = sum(
            simulate_basket(
                legs, D("10"), config=ShadowConfig(seed=s, level_vanish_probability=D("0"))
            ).complete
            for s in range(20)
        )
        harsh = sum(
            simulate_basket(
                legs, D("10"), config=ShadowConfig(seed=s, level_vanish_probability=D("1"))
            ).complete
            for s in range(20)
        )
        assert harsh <= gentle


class TestConfiguration:
    def test_the_unwind_haircut_scales_the_loss(self) -> None:
        legs = [leg("A", ("0.50", "100")), ("B", [])]
        small = simulate_basket(
            legs,
            D("10"),
            config=ShadowConfig(level_vanish_probability=D("0"), unwind_haircut=D("0.01")),
        )
        large = simulate_basket(
            legs,
            D("10"),
            config=ShadowConfig(level_vanish_probability=D("0"), unwind_haircut=D("0.10")),
        )
        assert large.unwind_loss > small.unwind_loss
