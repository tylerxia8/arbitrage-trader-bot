"""Exact, versioned venue fees (FR-010, EPIC-9).

Every candidate this system ever accepts must survive its fees, so the fee
model is the difference between a research toy and a trading decision. Three
rules govern it, and all three are enforced here rather than trusted:

**An unknown fee is never zero.** Asking for a fee with no effective rule
raises. The alternative -- defaulting to zero -- makes every unpriceable
market look like the most profitable one on the board, which is the exact
inversion you least want. A fee of zero is therefore something a rule must
*state*, never something it omits: ``maker_multiplier=None`` means "nobody
established this" and raises, and ``Decimal(0)`` means "the venue publishes no
maker fee for this series".

**Rules are versioned and cite their source.** A fee schedule is a document
that changes. A rule records where it came from and when it took effect, so a
past decision can be re-derived under the rule that was actually in force.

**Unverified rules cannot qualify anything.** A rule transcribed from a
secondary source may be used for research and is refused for qualification,
because the specification excludes third-party claims from controlling
requirements and a fee that is merely probably right is not a fee.

The general taker rule is now **verified**. It was transcribed from a 2022
CFTC filing and confirmed against the venue's current published fee schedule
on 2026-08-13, which is not merely a restatement: that page publishes the fee
*range* for a hundred contracts, and all four endpoints fall out of this
formula exactly. Taker at multiplier one gives $0.07 at a penny and $1.75 at
fifty cents; maker gives $0.02 and $0.44. Four independent numbers reproduced
to the cent confirm the coefficient, the ``P x (1 - P)`` shape and the
round-up-to-the-next-cent rule together, which no single quoted rate could.

Two structural consequences worth knowing before reading any net figure.

The fee **rounds up to the next cent, per trade, per leg**. A six-leg basket
therefore pays at least six cents in fees however small it is. At one contract
that is a six-cent hurdle before any edge counts at all; the fee only
amortises once size is real. Most of what looks like an opportunity dies here.

And **fees are charged on liquidity taken**. Crossing the spread to assemble a
basket -- which is what this system does, because a resting order is not an
arbitrage until it fills -- pays the taker rate on every leg. On the venue's
standard series there is no maker fee at all, so the entire fee burden modelled
here is a consequence of demanding immediacy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from arbbot.money import PAYOUT_DOLLARS, ZERO, quantize_cost, to_usd

__all__ = [
    "BASE_MAKER_RATE",
    "BASE_TAKER_RATE",
    "GENERAL_TRADING_FEE",
    "KALSHI_SCHEDULE",
    "FeeRule",
    "FeeSchedule",
    "Liquidity",
    "UnknownFeeError",
    "UnverifiedFeeError",
]


class UnknownFeeError(RuntimeError):
    """No effective fee rule covers this trade.

    Raised rather than returning zero. A candidate that cannot be priced must
    be rejected with ``unknown_fee``, never accepted at a fee of nothing.
    """


class UnverifiedFeeError(RuntimeError):
    """The rule exists but has not been confirmed against the primary source."""


class Liquidity(StrEnum):
    """Whether a trade took liquidity or provided it.

    Not a detail. On the venue's standard series the maker fee is nothing and
    the taker fee is the whole cost model, so this flag is the difference
    between a basket that clears its fees and one that does not. It defaults to
    ``TAKER`` everywhere, because that is what assembling a basket requires and
    the wrong default here would invent edge on every leg.
    """

    TAKER = "taker"
    MAKER = "maker"


#: Coefficient in ``rate x C x P x (1 - P)`` for liquidity taken, at multiplier
#: one. Confirmed by the published $0.07-$1.75 range for a hundred contracts.
BASE_TAKER_RATE: Final = Decimal("0.07")

#: The same for liquidity provided, on the series that charge for it at all.
#: Confirmed by the published $0.02-$0.44 range, and exactly a quarter of the
#: taker rate.
BASE_MAKER_RATE: Final = Decimal("0.0175")


@dataclass(frozen=True, slots=True)
class FeeRule:
    """One fee formula, with the provenance that makes it auditable.

    Expressed as a *multiplier* on the base rates rather than as a raw
    coefficient, because that is the vocabulary the venue's own schedule uses:
    it publishes one formula and a per-series multiplier. Encoding it the same
    way means a future series can be transcribed without re-deriving anything.
    """

    name: str
    multiplier: Decimal
    """Scales :data:`BASE_TAKER_RATE`. One for standard series."""

    maker_multiplier: Decimal | None
    """Scales :data:`BASE_MAKER_RATE`.

    ``Decimal(0)`` states that the venue publishes no maker fee for this
    series. ``None`` states that nobody has established what it is, and asking
    for a maker fee then raises :class:`UnknownFeeError` rather than quietly
    pricing the most attractive possibility.
    """

    source: str
    effective_from: dt.date
    verified: bool
    """Whether a human has confirmed this against the venue's own schedule.

    Until someone has, no candidate may be qualified on the rule -- research
    may use it, and the report must say what it rests on.
    """

    series_prefixes: tuple[str, ...] = ()
    """Tickers this rule overrides the general rate for. Empty means general."""

    def applies_to(self, ticker: str) -> bool:
        return not self.series_prefixes or ticker.startswith(self.series_prefixes)

    def rate(self, liquidity: Liquidity) -> Decimal:
        """Coefficient in force for this side of the trade."""
        if liquidity is Liquidity.TAKER:
            return BASE_TAKER_RATE * self.multiplier
        if self.maker_multiplier is None:
            raise UnknownFeeError(
                f"fee rule {self.name!r} does not establish a maker fee; the candidate "
                f"must be rejected with unknown_fee rather than priced at zero"
            )
        return BASE_MAKER_RATE * self.maker_multiplier

    def fee(
        self,
        price: Decimal,
        contracts: Decimal,
        *,
        liquidity: Liquidity = Liquidity.TAKER,
    ) -> Decimal:
        """Fee for one trade, rounded **up** to the next cent.

        Rounding up is the venue's rule and also the conservative direction:
        a fee understated by a fraction of a cent is edge invented out of
        arithmetic.
        """
        price = to_usd(price)
        contracts = to_usd(contracts)
        if contracts <= ZERO:
            return ZERO
        expected = self.rate(liquidity) * contracts * price * (PAYOUT_DOLLARS - price)
        return quantize_cost(expected)


#: The general event-contract trading fee.
#:
#: ``fees = round up(0.07 x C x P x (1-P))``, where round up means to the next
#: cent. Transcribed from Kalshi's fee schedule as filed with the CFTC on
#: 2022-09-12 and confirmed on 2026-08-13 against the venue's current published
#: schedule, whose "Most markets" row carries fee multiplier 1, no additional
#: fees, and a hundred-contract taker range of $0.07-$1.75 -- both endpoints
#: reproduced exactly by this formula.
#:
#: ``maker_multiplier`` is zero rather than unset: the published schedule lists
#: maker fees only for the series it enumerates as non-standard, and shows no
#: additional fees for everything else. Resting orders on a standard series are
#: free, which is a fact about the strategy space and not only about arithmetic.
GENERAL_TRADING_FEE: Final = FeeRule(
    name="kalshi-general",
    multiplier=Decimal("1"),
    maker_multiplier=ZERO,
    source=(
        "Kalshi published fee schedule, 'Most markets' row, confirmed 2026-08-13; "
        "originally CFTC filing rule091222kexdcm003, 2022-09-12"
    ),
    effective_from=dt.date(2022, 9, 12),
    verified=True,
)

#: Index markets carried half the general rate in the 2022 filing.
#:
#: Deliberately left unverified. The published schedule enumerates its
#: non-standard series across seventeen pages and this one was not among those
#: read, so the multiplier here rests on the 2022 filing alone. No market in the
#: current universe uses it; it exists to exercise the override path, and it
#: will refuse to qualify anything until someone checks it.
INDEX_TRADING_FEE: Final = FeeRule(
    name="kalshi-index",
    multiplier=Decimal("0.5"),
    maker_multiplier=None,
    source="Kalshi Fee Schedule, CFTC filing rule091222kexdcm003, 2022-09-12",
    effective_from=dt.date(2022, 9, 12),
    verified=False,
    series_prefixes=("INX", "NASDAQ100"),
)


class FeeSchedule:
    """Resolves the rule in force for a trade, and prices it."""

    def __init__(self, rules: tuple[FeeRule, ...]) -> None:
        # Specific before general, so an override is found first.
        self._rules = tuple(sorted(rules, key=lambda r: not r.series_prefixes))

    def rule_for(self, ticker: str, *, on: dt.date | None = None) -> FeeRule:
        """The rule covering ``ticker``.

        :raises UnknownFeeError: when nothing covers it. The caller must
            reject the candidate rather than assume a fee.
        """
        when = on or dt.date.today()  # noqa: DTZ011 -- a fee schedule is dated, not timed
        for rule in self._rules:
            if rule.applies_to(ticker) and rule.effective_from <= when:
                return rule
        raise UnknownFeeError(
            f"no fee rule in force for {ticker!r} on {when}; the candidate must be "
            f"rejected with unknown_fee rather than priced at zero"
        )

    def trade_fee(
        self,
        ticker: str,
        price: Decimal,
        contracts: Decimal,
        *,
        on: dt.date | None = None,
        require_verified: bool = False,
        liquidity: Liquidity = Liquidity.TAKER,
    ) -> Decimal:
        """Fee for trading ``contracts`` of ``ticker`` at ``price``.

        :param require_verified: qualification passes this. Research does not.
        :param liquidity: taker by default, because a basket is assembled by
            crossing the spread and pricing it as a maker would understate
            every leg to zero on the standard series.
        :raises UnverifiedFeeError: when qualification is attempted on a rule
            nobody has confirmed against the venue's published schedule.
        """
        rule = self.rule_for(ticker, on=on)
        if require_verified and not rule.verified:
            raise UnverifiedFeeError(
                f"fee rule {rule.name!r} is unverified (source: {rule.source}); "
                f"confirm it against the venue's current schedule before qualifying"
            )
        return rule.fee(price, contracts, liquidity=liquidity)

    def basket_fee(
        self,
        legs: list[tuple[str, Decimal]],
        contracts: Decimal,
        *,
        on: dt.date | None = None,
        require_verified: bool = False,
        liquidity: Liquidity = Liquidity.TAKER,
    ) -> Decimal:
        """Total fee to trade ``contracts`` of every leg.

        Summed per leg, because the rounding is per trade. Six legs at a penny
        each is six cents, and pricing the basket as one notional trade would
        understate it by most of its cost at small size.
        """
        return sum(
            (
                self.trade_fee(
                    ticker,
                    price,
                    contracts,
                    on=on,
                    require_verified=require_verified,
                    liquidity=liquidity,
                )
                for ticker, price in legs
            ),
            ZERO,
        )


#: The schedule in force. The general rule is confirmed against the venue's
#: current published schedule; the index override is not, and says so.
KALSHI_SCHEDULE: Final = FeeSchedule((GENERAL_TRADING_FEE, INDEX_TRADING_FEE))
