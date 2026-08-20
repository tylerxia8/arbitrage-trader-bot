"""Decoding Polymarket payloads into this system's vocabulary.

The second venue, and the one that shows which parts of the first were Kalshi
facts rather than facts about prediction markets. Three differences matter
enough to state.

**A binary market is two tokens, not one market with two sides.** Kalshi
publishes one book per market carrying resting bids on both sides, and the YES
ask is *derived* as ``$1.00 - best NO bid``. Polymarket publishes a separate
order book per outcome token, each with its own bids and asks, and the YES ask
is simply the YES ask. Code that assumed the derivation was how prediction
markets work would produce silently wrong prices here.

**Prices are decimal strings and the tick is not fixed.** Markets report
``orderPriceMinTickSize`` of either 0.01 or 0.001, so a price grid inferred
from one market is wrong for another. Everything stays :class:`~decimal.Decimal`
for the reason it does everywhere else in this project.

**The settlement rule is prose in a ``description`` field**, not a structured
strike. There is nothing to run a coverage check against, which is exactly the
situation the categorical verdict exists for: the machine records the text and
a person decides whether it means the same thing as the other venue's text.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Final

from arbbot.marketdata.types import BookSide, PriceLevel

__all__ = ["SCHEMA_VERSION", "VENUE", "parse_book", "parse_market"]

VENUE: Final = "polymarket"

#: Parser contract. Changes whenever decoding changes meaning (ADR-0005).
SCHEMA_VERSION: Final = "polymarket-clob-v1"


def _as_list(value: Any) -> list[Any]:
    """The venue returns some array fields as JSON-encoded strings."""
    if isinstance(value, str):
        try:
            return list(json.loads(value))
        except (ValueError, TypeError):
            return []
    return list(value or [])


def _levels(raw: Any, side: BookSide) -> list[tuple[BookSide, PriceLevel]]:
    out: list[tuple[BookSide, PriceLevel]] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            price, size = entry.get("price"), entry.get("size")
        else:
            price, size = entry[0], entry[1]
        if price is None or size is None:
            continue
        level = PriceLevel(Decimal(str(price)), Decimal(str(size)))
        if level.quantity > 0:
            out.append((side, level))
    return out


def parse_book(payload: dict[str, Any]) -> list[tuple[BookSide, PriceLevel]]:
    """Decode one outcome token's CLOB book.

    Both sides are taken as published. Unlike Kalshi, nothing is derived from
    the opposite side -- this venue quotes asks directly, and inventing them
    would misprice every market by its spread.
    """
    return _levels(payload.get("bids"), BookSide.YES) + _levels(payload.get("asks"), BookSide.NO)


def parse_market(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the fields this system reasons about.

    ``rules`` carries the whole ``description``. It is the only account of
    settlement the venue gives, it is prose, and a cross-venue pair lives or
    dies on whether it means the same as the other venue's prose -- so it is
    kept whole rather than summarised.
    """

    def dec(key: str) -> Decimal | None:
        raw = payload.get(key)
        return Decimal(str(raw)) if raw is not None else None

    return {
        "venue": VENUE,
        "market_id": str(payload.get("id", "")),
        "condition_id": str(payload.get("conditionId", "")),
        "question": str(payload.get("question", "")),
        "rules": str(payload.get("description", "")),
        "outcomes": [str(o) for o in _as_list(payload.get("outcomes"))],
        "token_ids": [str(t) for t in _as_list(payload.get("clobTokenIds"))],
        "end_date": payload.get("endDate"),
        "best_bid": dec("bestBid"),
        "best_ask": dec("bestAsk"),
        "tick_size": dec("orderPriceMinTickSize"),
        "liquidity": dec("liquidityNum"),
        "active": bool(payload.get("active")),
        "closed": bool(payload.get("closed")),
    }
