"""Shadow execution (FR-012, EPIC-11).

Simulates acquiring a basket the way it would actually be acquired: **one leg
at a time**, against a book that moves while you are doing it. The detector
prices a basket as though every leg could be bought at once; nothing can.

That gap is the entire risk of multi-leg arbitrage. Between the first fill and
the last, the price of a remaining leg can move, its depth can be taken by
someone else, or the whole opportunity can vanish -- and by then capital is
already committed to a partial position with no guaranteed payout. A partial
basket is not a smaller arbitrage. It is a directional bet nobody chose.

The simulation is deterministic. Given the same books, the same config, and
the same seed, it produces the same fills, because a falsification report
built on an irreproducible simulation is an anecdote. Randomness enters only
through an explicitly seeded generator, and only where the real world is
genuinely uncertain: whether a resting order is still there when you reach it.

What is modelled: per-leg latency, depth consumed by the fills already placed,
a configurable probability that a level has gone by the time the order lands,
partial fills, and the cost of unwinding what could not be completed.

What is not modelled, and must not be forgotten when reading a result: queue
position, other participants reacting to the trade, venue rejections, and the
possibility that the price moved *because* of the first leg. Every one of
those makes the real outcome worse than this, so a shadow result is an upper
bound on what execution would have achieved.
"""

from __future__ import annotations

import datetime as dt
import enum

# Only randrange() is used below, which returns an int. random.random() would
# put a float into a module that computes costs -- exactly what FR-002 forbids.
import random  # money-path: allow -- integer draws only, see _PROBABILITY_SCALE
from dataclasses import dataclass, field
from decimal import Decimal

from arbbot.economics.depth import walk_levels
from arbbot.marketdata.types import PriceLevel
from arbbot.money import ZERO, quantize_cost

__all__ = ["FillOutcome", "LegFill", "ShadowConfig", "ShadowResult", "simulate_basket"]

#: Probabilities are drawn as integers out of this, keeping floats out of a
#: module that computes costs.
_PROBABILITY_SCALE = 10_000


class FillOutcome(enum.StrEnum):
    """What happened to one leg."""

    FILLED = "filled"
    PARTIAL = "partial"
    """Some quantity acquired. The basket is now incomplete and exposed."""

    MISSED = "missed"
    """Nothing acquired: the level was gone when the order landed."""


@dataclass(frozen=True, slots=True)
class LegFill:
    """One leg's simulated execution."""

    ticker: str
    requested: Decimal
    filled: Decimal
    cost: Decimal
    outcome: FillOutcome

    @property
    def is_complete(self) -> bool:
        return self.outcome is FillOutcome.FILLED


@dataclass(slots=True)
class ShadowConfig:
    """Assumptions the simulation runs under. All of them make it worse."""

    leg_latency: dt.timedelta = dt.timedelta(milliseconds=250)
    """Time to place and confirm one leg. Legs are sequential, so a six-leg
    basket takes six times this before the last leg is even attempted."""

    level_vanish_probability: Decimal = Decimal("0.25")
    """Chance that the best level has gone when the order arrives.

    A guess, and stated as one. It is the single most consequential
    assumption here, so the stress runs vary it rather than trusting it.
    """

    unwind_haircut: Decimal = Decimal("0.02")
    """Fraction of value lost closing a leg that could not be completed.

    Crossing the spread back out, on a book that just moved against you.
    """

    seed: int = 0
    """Fixed by default. A falsification report built on an irreproducible
    simulation is an anecdote."""


@dataclass(slots=True)
class ShadowResult:
    """The outcome of trying to acquire one basket."""

    complete: bool
    fills: list[LegFill] = field(default_factory=list)
    acquisition_cost: Decimal = ZERO
    unwind_loss: Decimal = ZERO
    elapsed: dt.timedelta = dt.timedelta(0)

    @property
    def filled_legs(self) -> int:
        return sum(1 for fill in self.fills if fill.filled > ZERO)

    @property
    def exposed(self) -> bool:
        """Whether capital ended up in an incomplete basket.

        The failure mode that matters: not a smaller arbitrage, a directional
        position nobody chose.
        """
        return not self.complete and self.filled_legs > 0


def simulate_basket(
    legs: list[tuple[str, list[PriceLevel]]],
    quantity: Decimal,
    *,
    config: ShadowConfig | None = None,
    started: dt.datetime | None = None,
) -> ShadowResult:
    """Attempt to acquire ``quantity`` of every leg, sequentially.

    ``legs`` is ordered, and the order matters: the last leg is attempted
    after every preceding latency has elapsed, so it is the most exposed. A
    real executor would order legs by how likely they are to disappear;
    modelling that is Milestone 4's problem, and until then the caller's order
    stands.
    """
    settings = config or ShadowConfig()
    rng = random.Random(settings.seed)  # noqa: S311 -- simulation, not cryptography
    result = ShadowResult(complete=True)
    elapsed = dt.timedelta(0)

    for ticker, levels in legs:
        elapsed += settings.leg_latency

        available = list(levels)
        # The best level may be gone by the time the order lands. Dropping it
        # rather than repricing is the conservative reading: what remains is
        # what someone slower actually gets.
        #
        # Drawn as an integer out of 10,000 rather than a float, so no binary
        # floating point enters a module that computes costs (FR-002).
        draw = rng.randrange(_PROBABILITY_SCALE)
        threshold = int(settings.level_vanish_probability * _PROBABILITY_SCALE)
        if available and draw < threshold:
            available = available[1:]

        walk = walk_levels(available, quantity)
        if walk.filled <= ZERO:
            outcome = FillOutcome.MISSED
        elif walk.is_complete:
            outcome = FillOutcome.FILLED
        else:
            outcome = FillOutcome.PARTIAL

        result.fills.append(
            LegFill(
                ticker=ticker,
                requested=quantity,
                filled=walk.filled,
                cost=walk.cost,
                outcome=outcome,
            )
        )
        result.acquisition_cost += walk.cost
        if outcome is not FillOutcome.FILLED:
            result.complete = False

    result.elapsed = elapsed

    if not result.complete:
        # Everything acquired has to come back off, at a worse price than it
        # went on. This is the cost the gross figures never show.
        acquired = sum((fill.cost for fill in result.fills), ZERO)
        result.unwind_loss = quantize_cost(acquired * settings.unwind_haircut)

    return result
