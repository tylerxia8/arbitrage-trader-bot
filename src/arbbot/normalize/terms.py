"""Settlement-terms normalization and hashing (EPIC-5, FR-004).

The registry suspends a relationship when a leg's terms change. That mechanism
is only as good as the hash behind it, and a hash is a judgement about what
counts as *material* -- which is the whole difficulty.

Hash too much and the system cries wolf. Last price, volume and open interest
move constantly; folding them in would suspend every relationship on every
tick, and a suspension that fires continuously is one nobody reads.

Hash too little and it stays silent through the change that matters. If the
settlement source moved from one weather station to another, or a bucket's
bounds shifted by a degree, the basket may no longer be exhaustive while the
hash says nothing happened. That is the failure that costs money, and it is
silent by construction.

So the hashed set is deliberately narrow and deliberately explicit: the things
that determine *what resolves this contract*. Nothing about price, nothing
about liquidity, nothing about time-of-day. Adding a field here is a decision
to make the system more suspicious; removing one is a decision to make it
blinder, and both belong in review rather than in a refactor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "MATERIAL_FIELDS",
    "PARSER_VERSION",
    "NormalizedTerms",
    "normalize_kalshi_market",
]

#: Bumped whenever normalization changes meaning. Stored beside every hash so
#: a past decision can be re-derived under the parser that produced it.
PARSER_VERSION: Final = "terms-v1"

#: The fields whose change alters what resolves the contract.
#:
#: Ordered and closed on purpose. The hash is computed over exactly these, so
#: a venue adding a new field cannot silently change the hash, and a reviewer
#: reading this list knows precisely what they are being protected against.
MATERIAL_FIELDS: Final = (
    "ticker",
    "event_ticker",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "rules_primary",
    "settlement_source",
    "settlement_timer_seconds",
    "expiration_time",
    "custom_strike",
)


@dataclass(frozen=True, slots=True)
class NormalizedTerms:
    """A market's settlement terms, reduced to what determines resolution."""

    ticker: str
    fields: dict[str, Any]
    terms_hash: str
    parser_version: str = PARSER_VERSION

    def differs_from(self, other: NormalizedTerms) -> tuple[str, ...]:
        """Which material fields changed. For telling a reviewer *what* moved.

        A suspension that says only "terms changed" sends someone to diff two
        walls of legal text. Naming the field is the difference between a
        five-minute review and an afternoon.
        """
        return tuple(
            name for name in MATERIAL_FIELDS if self.fields.get(name) != other.fields.get(name)
        )


def _canonical(value: Any) -> Any:
    """Reduce a value to something that hashes stably.

    Strings are stripped and whitespace-collapsed: the venue reflows its rule
    text, and a line break moving must not read as a settlement change.
    """
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    if value is None:
        return None
    return str(value)


def normalize_kalshi_market(payload: dict[str, Any]) -> NormalizedTerms:
    """Extract and hash the material settlement terms of one market.

    :raises ValueError: if the payload has no ticker, since a hash that cannot
        say which contract it describes is not evidence of anything.
    """
    ticker = payload.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        raise ValueError("cannot normalize a market with no ticker")

    fields: dict[str, Any] = {}
    for name in MATERIAL_FIELDS:
        if name == "settlement_source":
            # Not a field the venue exposes directly; the rules text names the
            # agency and report. Kept as its own key so that if the venue ever
            # does expose it, it slots in without changing the hash's meaning.
            fields[name] = _canonical(payload.get("settlement_source"))
        else:
            fields[name] = _canonical(payload.get(name))

    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return NormalizedTerms(ticker=ticker, fields=fields, terms_hash=digest)
