"""Read the archive for moments when a basket priced below its payout.

**This is a research read, not detection.** It does not qualify anything, and
nothing it reports may be traded. The M2 detector will require a human-approved
relationship (FR-005), an exact fee model (FR-010), and a depth walk (FR-009);
this has none of those. What it does is answer the question a collection run is
actually for: is anything showing up at all, and how big is it?

Three safeguards, each of them a mistake made while writing this:

**Legs must be simultaneous.** The first version held every leg's last-seen
price indefinitely and reported a Boston basket at $0.38 -- a 62% edge on a
guaranteed dollar. It was combining a 16:06 quote with a 20:44 one on a
same-day market that reprices all day. Priced consistently the same basket was
$1.12. Stale legs do not merely add noise; they manufacture spectacular edges
out of quotes that never coexisted.

**Size is reported, not just price.** A Philadelphia basket at $0.84 looks like
a 16% edge until you notice the discount lives entirely in one leg quoted for
four contracts. The whole thing is worth 64 cents gross. Reporting cost without
capacity is how pennies get mistaken for a business.

**Leg sets are inferred, and that is a stated weakness.** The archive records
what was collected, not what the venue listed. If a leg was missing, summing
the rest understates the basket and invents a discount. This uses the largest
leg set ever seen per event and reports the count so a reader can check it, but
it cannot prove completeness. Only the approved registry will.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.db.models import BookSnapshot
from arbbot.fees import KALSHI_2022_SCHEDULE, FeeSchedule
from arbbot.money import PAYOUT_DOLLARS, ZERO

__all__ = [
    "DEFAULT_MAX_LEG_AGE",
    "MIN_LEGS",
    "BasketEpisode",
    "BasketObservation",
    "ScanResult",
    "event_of",
    "scan_baskets",
]

#: How far apart two legs' quotes may be and still count as one basket.
#:
#: Deliberately far tighter than it is tempting to set it. The polled archive
#: carries 0-30 second book age, so a generous window would sweep in most of
#: the run -- and every second of slack is a second in which the price being
#: summed had already gone.
DEFAULT_MAX_LEG_AGE: Final = dt.timedelta(seconds=60)

#: Fewest legs that can constitute a basket at all.
#:
#: A single market priced below a dollar is not an arbitrage, it is a contract
#: trading below a dollar -- which is the ordinary state of almost every
#: contract. Without this floor a stray single-leg event reports as a 99%
#: edge, which is both wrong and the most eye-catching row in the table.
MIN_LEGS: Final = 2


def event_of(ticker: str) -> str:
    """``KXHIGHTATL-26AUG13-T92`` -> ``KXHIGHTATL-26AUG13``."""
    return ticker.rsplit("-", 1)[0]


def _best_yes_ask(no_levels: dict[str, str]) -> tuple[Decimal, Decimal] | None:
    """Cheapest price to buy YES, and the size available there.

    Derived from the opposite side: a NO bid at $0.79 is an offer to sell YES
    at $0.21. Returns ``None`` when nobody is offering, in which case the
    basket cannot be assembled at any price.
    """
    if not no_levels:
        return None
    best_bid = max(Decimal(price) for price in no_levels)
    return PAYOUT_DOLLARS - best_bid, Decimal(no_levels[str(best_bid)])


@dataclass(frozen=True, slots=True)
class BasketObservation:
    """One moment at which a full leg set priced below its guaranteed payout."""

    event: str
    observed_ts: dt.datetime
    legs: int
    cost: Decimal
    max_contracts: Decimal
    """Smallest depth across the legs -- the basket cannot exceed it."""

    fee: Decimal = ZERO
    """Estimated venue fee at ``max_contracts``, summed per leg.

    Estimated, not authoritative: the rule is transcribed from a 2022
    regulatory filing and nobody has confirmed it is still in force, so
    :mod:`arbbot.fees` refuses to qualify anything on it.
    """

    @property
    def gross_edge(self) -> Decimal:
        """Per-basket discount to the payout. Gross: no fees, no slippage."""
        return PAYOUT_DOLLARS - self.cost

    @property
    def gross_dollars(self) -> Decimal:
        """What the whole thing is worth at the depth available."""
        return self.gross_edge * self.max_contracts

    @property
    def net_dollars(self) -> Decimal:
        """Gross less the estimated fee. Still before slippage and latency.

        This is the number that decides things, and it is usually negative.
        The fee rounds up to a cent per leg, so a six-leg basket pays at least
        six cents however small it is -- which is more than most of these are
        worth in total.
        """
        return self.gross_dollars - self.fee


@dataclass(slots=True)
class BasketEpisode:
    """A continuous stretch during which one event priced below its payout.

    Consecutive observations are collapsed because every leg update reprices
    the basket, so a single opportunity sitting untouched for four minutes
    appears as dozens of identical rows. Counting those as separate findings
    would inflate "how often does this happen" by however fast the collector
    happened to be polling.

    Duration is the point. Hypothesis two in the specification is that
    inconsistencies *persist long enough to act on*, and its failure signal is
    that they vanish before anything could be done. An episode is the unit
    that question is asked of.
    """

    event: str
    first_seen: dt.datetime
    last_seen: dt.datetime
    legs: int
    best_cost: Decimal
    best_dollars: Decimal
    best_net: Decimal
    max_contracts: Decimal
    observations: int = 1

    @property
    def duration(self) -> dt.timedelta:
        return self.last_seen - self.first_seen

    @property
    def best_edge(self) -> Decimal:
        return PAYOUT_DOLLARS - self.best_cost


@dataclass(slots=True)
class ScanResult:
    """Everything the scan saw."""

    episodes: list[BasketEpisode] = field(default_factory=list)
    observations: list[BasketObservation] = field(default_factory=list)
    priced: int = 0
    skipped_stale: int = 0
    skipped_incomplete: int = 0
    events_seen: int = 0

    @property
    def best(self) -> BasketEpisode | None:
        """Largest net dollars, not the lowest price.

        A cheap basket nobody can size into is worth less than a dear one with
        depth, and ranking by price alone puts the four-contract curiosity at
        the top of the report.
        """
        return max(self.episodes, key=lambda e: e.best_net, default=None)

    def render(self, limit: int = 15) -> str:
        lines = [
            f"basket pricings evaluated : {self.priced:,}",
            f"rejected, stale legs      : {self.skipped_stale:,}",
            f"rejected, incomplete set  : {self.skipped_incomplete:,}",
            f"events observed           : {self.events_seen}",
            f"episodes below payout     : {len(self.episodes):,}",
        ]
        if not self.episodes:
            lines.append("")
            lines.append("nothing priced below its payout.")
            return "\n".join(lines)

        survivors = [e for e in self.episodes if e.best_net > ZERO]
        lines.append(f"still positive after fees  : {len(survivors):,}")

        lines.append("")
        gross_header, net_header = "gross $", "net $"
        lines.append(
            f"{'event':<26} {'legs':>4} {'best':>8} {'size':>8} "
            f"{gross_header:>8} {net_header:>8} {'lasted':>8}  first seen"
        )
        lines.append("-" * 92)
        for ep in sorted(self.episodes, key=lambda e: -e.best_net)[:limit]:
            lines.append(
                f"{ep.event:<26} {ep.legs:>4} ${ep.best_cost:>7} {ep.max_contracts:>8} "
                f"${ep.best_dollars:>7.2f} ${ep.best_net:>7.2f} "
                f"{_duration(ep.duration):>8}  {ep.first_seen:%Y-%m-%d %H:%M:%S}"
            )

        gross_total = sum((e.best_dollars for e in self.episodes), ZERO)
        net_total = sum((e.best_net for e in self.episodes), ZERO)
        positive = sum((e.best_net for e in survivors), ZERO)
        lines.append("")
        lines.append(f"every episode at its best, gross : ${gross_total:>8.2f}")
        lines.append(f"                          net    : ${net_total:>8.2f}")
        lines.append(f"only the ones still positive     : ${positive:>8.2f}")
        lines.append("")
        lines.append("Fees are ESTIMATED from a 2022 regulatory filing that nobody has")
        lines.append("confirmed is still in force; arbbot.fees refuses to qualify on it.")
        lines.append("Slippage, latency and capital cost are not modelled. Only the best")
        lines.append("price level is used, and no relationship here has been approved.")
        return "\n".join(lines)


def _duration(delta: dt.timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def scan_baskets(
    session: Session,
    *,
    max_leg_age: dt.timedelta = DEFAULT_MAX_LEG_AGE,
    since: dt.datetime | None = None,
    fees: FeeSchedule = KALSHI_2022_SCHEDULE,
) -> ScanResult:
    """Walk the archive and record every full, fresh set priced below payout."""
    stmt = select(
        BookSnapshot.ticker,
        BookSnapshot.captured_ts,
        BookSnapshot.no_levels,
        BookSnapshot.is_complete,
    ).order_by(BookSnapshot.captured_ts, BookSnapshot.id)
    if since is not None:
        stmt = stmt.where(BookSnapshot.captured_ts >= since)

    rows = session.execute(stmt).all()

    # The expected leg set is the largest ever seen for the event. Inferred,
    # not authoritative -- see the module docstring.
    legs_by_event: dict[str, set[str]] = defaultdict(set)
    for ticker, *_ in rows:
        legs_by_event[event_of(ticker)].add(ticker)

    result = ScanResult(events_seen=len(legs_by_event))
    latest: dict[str, tuple[Decimal, Decimal, dt.datetime]] = {}
    open_episodes: dict[str, BasketEpisode] = {}

    for ticker, captured, no_levels, is_complete in rows:
        if not is_complete:
            continue

        quote = _best_yes_ask(no_levels or {})
        if quote is None:
            latest.pop(ticker, None)
            continue
        ask, size = quote
        latest[ticker] = (ask, size, captured)

        event = event_of(ticker)
        legs = legs_by_event[event]
        if len(legs) < MIN_LEGS:
            continue
        if not legs <= latest.keys():
            result.skipped_incomplete += 1
            continue

        quotes = [latest[leg] for leg in legs]
        if any(captured - when > max_leg_age for _, _, when in quotes):
            result.skipped_stale += 1
            continue

        result.priced += 1
        cost = sum((ask for ask, _, _ in quotes), ZERO)
        if cost >= PAYOUT_DOLLARS:
            # The event priced at or above payout, so any run of cheapness has
            # ended. The next one is a separate episode.
            open_episodes.pop(event, None)
            continue

        size = min(s for _, s, _ in quotes)
        observation = BasketObservation(
            event=event,
            observed_ts=captured,
            legs=len(legs),
            cost=cost,
            max_contracts=size,
            fee=fees.basket_fee([(leg, latest[leg][0]) for leg in legs], size),
        )
        result.observations.append(observation)

        episode = open_episodes.get(event)
        if episode is None:
            episode = BasketEpisode(
                event=event,
                first_seen=captured,
                last_seen=captured,
                legs=observation.legs,
                best_cost=observation.cost,
                best_dollars=observation.gross_dollars,
                best_net=observation.net_dollars,
                max_contracts=observation.max_contracts,
            )
            open_episodes[event] = episode
            result.episodes.append(episode)
        else:
            episode.last_seen = captured
            episode.observations += 1
            if observation.net_dollars > episode.best_net:
                # Ranked on net, not gross: the best moment of an episode is
                # the one that survives its fees, not the cheapest headline.
                episode.best_net = observation.net_dollars
                episode.best_dollars = observation.gross_dollars
                episode.best_cost = observation.cost
                episode.max_contracts = observation.max_contracts

    return result
