"""Exact, versioned venue fees (FR-010, EPIC-9).

Every candidate this system ever accepts must survive its fees, so the fee
model is the difference between a research toy and a trading decision. Three
rules govern it, and all three are enforced here rather than trusted:

**An unknown fee is never zero.** Asking for a fee with no effective rule
raises. The alternative -- defaulting to zero -- makes every unpriceable
market look like the most profitable one on the board, which is the exact
inversion you least want.

**Rules are versioned and cite their source.** A fee schedule is a document
that changes. A rule records where it came from and when it took effect, so a
past decision can be re-derived under the rule that was actually in force.

**Unverified rules cannot qualify anything.** A rule transcribed from a
secondary source may be used for research and is refused for qualification,
because the specification excludes third-party claims from controlling
requirements and a fee that is merely probably right is not a fee.

The structural consequence worth knowing before reading any net figure: the
fee **rounds up to the next cent, per trade, per leg**. A six-leg basket
therefore pays at least six cents in fees however small it is. At one contract
that is a six-cent hurdle before any edge counts at all; the fee only
amortises once size is real. Most of what looks like an opportunity dies here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from arbbot.money import PAYOUT_DOLLARS, ZERO, quantize_cost, to_usd

__all__ = [
    "GENERAL_TRADING_FEE",
    "KALSHI_2022_SCHEDULE",
    "FeeRule",
    "FeeSchedule",
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


@dataclass(frozen=True, slots=True)
class FeeRule:
    """One fee formula, with the provenance that makes it auditable."""

    name: str
    rate: Decimal
    """Coefficient in ``rate x C x P x (1 - P)``."""

    source: str
    effective_from: dt.date
    verified: bool
    """Whether a human has confirmed this against the venue's own schedule.

    The transcription here comes from Kalshi's fee schedule as filed with the
    CFTC, which is a primary source -- but a 2022 one. Whether it is still the
    schedule in force is a question for the owner, not for this module, and
    until someone answers it no candidate may be qualified on this rule.
    """

    series_prefixes: tuple[str, ...] = ()
    """Tickers this rule overrides the general rate for. Empty means general."""

    def applies_to(self, ticker: str) -> bool:
        return not self.series_prefixes or ticker.startswith(self.series_prefixes)

    def fee(self, price: Decimal, contracts: Decimal) -> Decimal:
        """Fee for one trade, rounded **up** to the next cent.

        Rounding up is the venue's rule and also the conservative direction:
        a fee understated by a fraction of a cent is edge invented out of
        arithmetic.
        """
        price = to_usd(price)
        contracts = to_usd(contracts)
        if contracts <= ZERO:
            return ZERO
        expected = self.rate * contracts * price * (PAYOUT_DOLLARS - price)
        return quantize_cost(expected)


#: The general event-contract trading fee.
#:
#: Quoted from Kalshi's fee schedule as filed with the CFTC on 2022-09-12:
#: "fees = round up(0.07 x C x P x (1-P))", where "round up = rounds to the
#: next cent". The same filing states there is no settlement fee and no
#: processing fee, and that fees are charged only on orders "immediately
#: matched" -- so a basket bought by crossing the spread pays them in full.
GENERAL_TRADING_FEE: Final = FeeRule(
    name="kalshi-general-taker",
    rate=Decimal("0.07"),
    source="Kalshi Fee Schedule, CFTC filing rule091222kexdcm003, 2022-09-12",
    effective_from=dt.date(2022, 9, 12),
    verified=False,
)

#: Index markets carry half the general rate in the same filing. Encoded to
#: exercise the override path; no market in the current universe uses it.
INDEX_TRADING_FEE: Final = FeeRule(
    name="kalshi-index-taker",
    rate=Decimal("0.035"),
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
    ) -> Decimal:
        """Fee for buying ``contracts`` of ``ticker`` at ``price``.

        :param require_verified: qualification passes this. Research does not.
        :raises UnverifiedFeeError: when qualification is attempted on a rule
            nobody has confirmed against the venue's published schedule.
        """
        rule = self.rule_for(ticker, on=on)
        if require_verified and not rule.verified:
            raise UnverifiedFeeError(
                f"fee rule {rule.name!r} is unverified (source: {rule.source}); "
                f"confirm it against the venue's current schedule before qualifying"
            )
        return rule.fee(price, contracts)

    def basket_fee(
        self,
        legs: list[tuple[str, Decimal]],
        contracts: Decimal,
        *,
        on: dt.date | None = None,
        require_verified: bool = False,
    ) -> Decimal:
        """Total fee to buy ``contracts`` of every leg.

        Summed per leg, because the rounding is per trade. Six legs at a penny
        each is six cents, and pricing the basket as one notional trade would
        understate it by most of its cost at small size.
        """
        return sum(
            (
                self.trade_fee(ticker, price, contracts, on=on, require_verified=require_verified)
                for ticker, price in legs
            ),
            ZERO,
        )


#: The schedule as filed in 2022. Unverified against the current published one.
KALSHI_2022_SCHEDULE: Final = FeeSchedule((GENERAL_TRADING_FEE, INDEX_TRADING_FEE))
