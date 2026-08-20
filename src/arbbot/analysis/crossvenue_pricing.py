"""What a cross-venue pair actually costs, and what it earns per year.

Buying YES on one venue and NO on another pays exactly a dollar whichever way
the world goes, so the edge is ``1.00 - (yes_ask + no_ask)``. That subtraction
is the easy part and it is not the number that decides anything.

**Annualised return is the headline, because absolute edge lied.** The first
cross-venue probe found three and a bit cents on a pair settling in 2028.
Three cents on ninety-seven, held two and a half years, is under one percent a
year -- worse than leaving the money alone, before any risk. A report that led
with "3.3 cents" would have read as a discovery. The same three cents on a
contract resolving next week is a different business entirely, and only the
calendar tells them apart.

**Fees are asymmetric and only one side is known.** Kalshi's taker rule is
confirmed and charges ``ceil(0.07 x C x P x (1-P))`` with a one-cent floor per
contract, which takes about a third of a three-cent edge on a ten-cent leg. The
other venue reports fee fields in units this project has not confirmed, so they
are reported as unknown rather than guessed at -- the same rule Kalshi's own
schedule was held to before anybody read it.

**Nothing here is tradeable and the report says so on every row.** A price
means a guaranteed dollar only if the two contracts settle on the same event,
and that is a claim about prose that no arithmetic in this module can check.
Until a reviewer has approved the pair, a cheap row is a question.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from arbbot.fees import KALSHI_SCHEDULE, FeeSchedule
from arbbot.money import PAYOUT_DOLLARS, ZERO, quantize_cost

__all__ = ["CrossVenueQuote", "CrossVenueReport", "price_pair"]

#: Below this, an annualised figure is arithmetic rather than information.
#:
#: A pair resolving in three days turns a one-cent edge into a headline rate of
#: several hundred percent, which is true and useless: nothing can be
#: compounded at it, the fill is one-shot, and the number exists mostly to
#: flatter whoever is reading. Short-dated pairs are still reported -- with the
#: rate suppressed rather than the row hidden.
MIN_DAYS_FOR_RATE = 7


@dataclass(frozen=True, slots=True)
class CrossVenueQuote:
    """One pair, priced at one moment."""

    slug: str
    kalshi_ticker: str
    other_venue: str
    other_id: str
    question: str
    kalshi_yes_ask: Decimal
    other_no_ask: Decimal
    quantity: Decimal
    resolves: dt.datetime | None
    approved: bool = False
    fees: FeeSchedule = KALSHI_SCHEDULE

    @property
    def cost(self) -> Decimal:
        """Cost of a guaranteed dollar, before fees."""
        return self.kalshi_yes_ask + self.other_no_ask

    @property
    def gross_edge(self) -> Decimal:
        return PAYOUT_DOLLARS - self.cost

    @property
    def kalshi_fee(self) -> Decimal:
        """Confirmed taker fee on the Kalshi leg only.

        The other venue's fee is unknown, so this understates total cost. Every
        figure downstream is therefore an upper bound on the edge, which is the
        direction to be wrong in.
        """
        return self.fees.trade_fee(
            self.kalshi_ticker, self.kalshi_yes_ask, self.quantity, require_verified=True
        )

    @property
    def net_edge(self) -> Decimal:
        """Edge after the fee that is known. An upper bound."""
        return quantize_cost(self.gross_edge * self.quantity - self.kalshi_fee)

    @property
    def divergence(self) -> Decimal:
        """How far apart the two venues price the same claim.

        Both sides expressed as a YES probability. Venues quoting genuinely the
        same event differ by a spread and a little noise. A gap of tens of
        points is not an inefficiency somebody left lying about -- it is two
        different questions.
        """
        other_yes = PAYOUT_DOLLARS - self.other_no_ask
        return abs(self.kalshi_yes_ask - other_yes)

    @property
    def suspect(self) -> bool:
        """Whether the gap is better explained by a mismatch than an edge.

        Fifteen points is far outside any spread either venue quotes and far
        inside the gaps mismatched pairs produce: the national-House-control
        against IN-08-district pairing that prompted this measured eighty-two.
        """
        return self.divergence > Decimal("0.15")

    @property
    def capital(self) -> Decimal:
        return quantize_cost(self.cost * self.quantity)

    def days_to_resolution(self, now: dt.datetime) -> int | None:
        if self.resolves is None:
            return None
        return max((self.resolves - now).days, 0)

    def annualised(self, now: dt.datetime) -> Decimal | None:
        """Net edge as a yearly rate on the capital it locks up.

        ``None`` when the horizon is unknown or too short for the figure to
        mean anything. Simple, not compounded: this is one position held to
        settlement, and compounding would assume a pipeline of replacements
        that nothing here has demonstrated exists.
        """
        days = self.days_to_resolution(now)
        if days is None or days < MIN_DAYS_FOR_RATE or self.capital <= ZERO:
            return None
        return (self.net_edge / self.capital) * Decimal(365) / Decimal(days)


@dataclass(slots=True)
class CrossVenueReport:
    """Every pair priced in one pass."""

    quotes: list[CrossVenueQuote] = field(default_factory=list)
    priced_at: dt.datetime | None = None
    skipped_unquoted: int = 0

    @property
    def positive(self) -> list[CrossVenueQuote]:
        return [q for q in self.quotes if q.net_edge > ZERO]

    @property
    def credible(self) -> list[CrossVenueQuote]:
        """Positive, and not obviously a pair of different questions."""
        return [q for q in self.positive if not q.suspect]

    @property
    def mismatched(self) -> list[CrossVenueQuote]:
        return [q for q in self.positive if q.suspect]

    def render(self, limit: int = 20) -> str:
        now = self.priced_at or dt.datetime.now(dt.UTC)
        lines = [
            f"pairs priced          : {len(self.quotes)}",
            f"  one side unquoted   : {self.skipped_unquoted}",
            f"positive after Kalshi fees : {len(self.positive)}",
            f"approved for trading  : {sum(1 for q in self.quotes if q.approved)}",
        ]
        lines.append(f"  probably mismatched : {len(self.mismatched)}")
        lines.append(f"  credible            : {len(self.credible)}")

        if self.mismatched:
            lines.append("")
            lines.append("PROBABLY NOT THE SAME CLAIM -- the two venues disagree too much:")
            lines.append(f"  {'pair':<44} {'cost':>8} {'gap':>7}")
            lines.append("  " + "-" * 62)
            for q in sorted(self.mismatched, key=lambda q: -q.divergence)[:8]:
                lines.append(f"  {q.question[:43]:<44} ${q.cost:>7} {q.divergence:>6.0%}")
            lines.append("")
            lines.append("  Venues quoting the same event differ by a spread, not by tens of")
            lines.append("  points. The wider the gap the better the arithmetic looks, so the")
            lines.append("  worst pairs sort to the top of any report ordered by edge -- one")
            lines.append("  of these matched national House control against one district.")

        if not self.credible:
            lines.append("")
            lines.append("No pair both covers its Kalshi fee and prices consistently across")
            lines.append("the two venues.")
            return "\n".join(lines)

        lines.append("")
        lines.append(f"  {'pair':<40} {'cost':>8} {'net':>9} {'days':>6} {'per year':>9}")
        lines.append("  " + "-" * 78)
        for q in sorted(self.credible, key=lambda q: -(q.annualised(now) or ZERO))[:limit]:
            rate = q.annualised(now)
            days = q.days_to_resolution(now)
            lines.append(
                f"  {q.question[:39]:<40} ${q.cost:>7} ${q.net_edge:>8} "
                f"{days if days is not None else '?':>6} "
                f"{f'{rate:.1%}' if rate is not None else 'n/a':>9}"
            )

        lines.append("")
        lines.append("Per-year is simple, not compounded: one position held to settlement.")
        lines.append("Compounding would assume a pipeline of replacements nothing here has")
        lines.append("shown exists. Rates are suppressed under a week, where the arithmetic")
        lines.append("produces large numbers that no capital can actually be paid.")
        lines.append("")
        lines.append("Net is after the CONFIRMED Kalshi taker fee only. The other venue's")
        lines.append("fee schedule is in units this project has not verified, so every")
        lines.append("figure above is an upper bound on the edge.")
        lines.append("")
        unapproved = [q for q in self.credible if not q.approved]
        if unapproved:
            lines.append(f"{len(unapproved)} of these have no approved relationship behind them.")
            lines.append("A price is a guaranteed dollar only if both contracts settle on the")
            lines.append("same event, which is a claim about prose that no arithmetic here")
            lines.append("checks. Until a reviewer signs, a cheap row is a question.")
        return "\n".join(lines)


def price_pair(
    *,
    slug: str,
    kalshi_ticker: str,
    other_venue: str,
    other_id: str,
    question: str,
    kalshi_yes_ask: Decimal | None,
    other_yes_bid: Decimal | None,
    quantity: Decimal,
    resolves: dt.datetime | None,
    approved: bool = False,
    fees: FeeSchedule = KALSHI_SCHEDULE,
) -> CrossVenueQuote | None:
    """Build a quote, or ``None`` when either side cannot be bought.

    ``other_yes_bid`` is the other venue's best YES bid; buying its NO costs
    ``1.00 - that``, because the two outcome tokens are complements and an
    offer to buy YES at a price is an offer to sell NO at the remainder. A
    missing quote on either side means the pair cannot be assembled at all,
    which is a different thing from it being expensive.
    """
    if kalshi_yes_ask is None or other_yes_bid is None:
        return None
    return CrossVenueQuote(
        slug=slug,
        kalshi_ticker=kalshi_ticker,
        other_venue=other_venue,
        other_id=other_id,
        question=question,
        kalshi_yes_ask=kalshi_yes_ask,
        other_no_ask=PAYOUT_DOLLARS - other_yes_bid,
        quantity=quantity,
        resolves=resolves,
        approved=approved,
        fees=fees,
    )
