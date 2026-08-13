"""Structural classification of Kalshi event groups.

Answers "could this set of outcomes form an exhaustive basket?" -- a filter for
proposing candidates, never an approval. FR-005 requires a human to read the
settlement terms and sign; nothing here substitutes for that, and the most
important thing this module does is refuse to look like it might.

**The trap this exists to avoid.** Kalshi flags events ``mutually_exclusive``,
which is exactly the attribute an exhaustive-basket detector seems to want.
It is not sufficient, and using it alone is actively dangerous. The flag
asserts that *at most* one outcome wins. A basket needs *exactly* one: at most
one AND at least one.

Surveying live markets on 2026-08-13, four mutually-exclusive events priced
below a dollar, the cheapest at $0.746 -- an apparent 25% edge on a guaranteed
dollar. Every one enumerated named candidates out of an unbounded space
("Buttigieg v. Vance", one of sixteen listed 2028 matchups). The missing
$0.254 was the market correctly pricing the chance that none of the listed
outcomes occurs. Buying all sixteen legs returns nothing in that case: not a
25% gain, a total loss on the basket.

The discriminator is ``strike_type``. Numeric buckets (``between``, with
``less`` and ``greater`` tails) tile a real line and are exhaustive by
construction -- the underlying is a number, and every number falls in exactly
one bucket. ``custom`` legs enumerate names and cover only what someone chose
to list.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

__all__ = ["NUMERIC_STRIKE_TYPES", "EventStructure", "StructuralVerdict", "classify_event"]

#: Strike types that describe a numeric bucket rather than a named candidate.
NUMERIC_STRIKE_TYPES = frozenset({"between", "less", "greater"})


class StructuralVerdict(enum.StrEnum):
    """Whether an outcome set is *shaped* like an exhaustive basket."""

    PARTITION = "partition"
    """Numeric buckets with both tails. A candidate for human review -- the
    only verdict that may be proposed to the registry."""

    ENUMERATED = "enumerated"
    """Named candidates from an unbounded space. Mutually exclusive but not
    exhaustive; the apparent discount is the price of the missing outcomes."""

    OPEN_ENDED = "open_ended"
    """Numeric buckets missing a tail, so values beyond the last bucket
    resolve nothing and the basket pays zero when they occur."""

    MIXED = "mixed"
    """Numeric and named legs together, or strike types this build does not
    recognise. Unclassifiable without reading the terms."""

    TOO_FEW = "too_few"
    """Fewer outcomes than a basket needs."""

    @property
    def may_propose(self) -> bool:
        """Whether a candidate may be drafted from this set.

        Proposing is not approving. A drafted relationship enters the registry
        as PENDING and cannot qualify anything until a person signs for it.
        """
        return self is StructuralVerdict.PARTITION


@dataclass(frozen=True, slots=True)
class EventStructure:
    """What a structural scan concluded about one event group."""

    event_ticker: str
    verdict: StructuralVerdict
    reason: str
    outcomes: int
    tickers: tuple[str, ...]

    @property
    def may_propose(self) -> bool:
        return self.verdict.may_propose


def classify_event(
    event: dict[str, Any], markets: list[dict[str, Any]], *, min_outcomes: int = 3
) -> EventStructure:
    """Classify one event's outcome set.

    ``markets`` should already be filtered to tradeable outcomes; a settled or
    unopened leg cannot be bought and would misrepresent the set.
    """
    ticker = str(event.get("event_ticker", ""))
    tickers = tuple(str(m.get("ticker", "")) for m in markets)

    def structure(verdict: StructuralVerdict, reason: str) -> EventStructure:
        return EventStructure(ticker, verdict, reason, len(markets), tickers)

    if len(markets) < min_outcomes:
        return structure(StructuralVerdict.TOO_FEW, f"{len(markets)} outcomes")

    if not event.get("mutually_exclusive"):
        # Without exclusivity two outcomes can both pay, so the set is not a
        # partition even if every leg is numeric.
        return structure(StructuralVerdict.MIXED, "venue does not mark the outcomes exclusive")

    types = {m.get("strike_type") for m in markets}

    if types <= {"custom"}:
        return structure(
            StructuralVerdict.ENUMERATED,
            "named candidates from an unbounded space; not collectively exhaustive",
        )

    if not types <= NUMERIC_STRIKE_TYPES:
        listed = sorted(str(t) for t in types if t)
        return structure(StructuralVerdict.MIXED, f"strike types {listed or ['unset']}")

    missing = []
    if not any(m.get("strike_type") == "less" for m in markets):
        missing.append("lower")
    if not any(m.get("strike_type") == "greater" for m in markets):
        missing.append("upper")
    if missing:
        return structure(
            StructuralVerdict.OPEN_ENDED,
            f"no {' and '.join(missing)} tail bucket; values beyond it resolve nothing",
        )

    return structure(
        StructuralVerdict.PARTITION,
        "numeric buckets with both tails -- propose for human review of the boundaries",
    )
