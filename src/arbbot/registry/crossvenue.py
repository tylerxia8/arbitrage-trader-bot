"""Proposing that two venues are pricing the same claim (FR-005).

Buying YES on one venue and NO on another pays exactly a dollar whichever way
the world goes -- provided the two contracts settle on the same event. That
proviso is the entire trade. Everything else here is arithmetic.

**Which is why nothing in this module decides anything.** It finds candidates
and puts both settlement texts in front of a person. The first automated pass
over these two venues paired "Who will *run for* the 2028 Republican
nomination?" with "Will X *win* the 2028 Republican nomination?" at 0.86 token
similarity. Running is far likelier than winning, so the price gap is large,
and it reads exactly like an enormous arbitrage while being a large directional
bet. Title matching is not a heuristic to be tightened later. It is the failure
mode, and the only defence against it is that a person reads both texts.

Two consequences.

**A proposal carries both venues' prose, verbatim and side by side.** Not a
summary, and not a similarity score standing in for a judgement. The reviewer
is being asked one question -- do these resolve identically in *every* state --
and cannot answer it from anything less than the words.

**Similarity ranks candidates and never qualifies them.** It decides what a
person looks at first. A pair scoring 1.0 with unread terms is worth precisely
what one scoring 0.6 with unread terms is worth: nothing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from arbbot.normalize.terms import normalize_kalshi_market
from arbbot.registry.service import RelationshipRegistry
from arbbot.relationships import RelationshipType

__all__ = [
    "CrossVenueCandidate",
    "find_candidates",
    "propose_pairs",
    "similarity",
]

#: Words too common to carry meaning when comparing two questions.
_STOP = frozenset(
    {
        "will",
        "the",
        "be",
        "a",
        "an",
        "in",
        "on",
        "of",
        "to",
        "by",
        "before",
        "after",
        "at",
        "for",
        "is",
        "and",
        "or",
        "who",
        "what",
        "which",
        "this",
        "that",
        "it",
        "next",
        "us",
    }
)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 2}


def similarity(left: str, right: str) -> float:  # money-path: allow -- a text score
    """Jaccard overlap of two questions, for ordering a review queue.

    Deliberately crude, and deliberately not load-bearing. A sharper measure
    would order the queue slightly better and would not make any pair safe to
    trade -- what separates "run for" from "win" is not lexical distance. Those
    two questions are nearly identical as text and describe different events.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0  # money-path: allow -- a text score, not a price
    return len(a & b) / len(a | b)


def _hash_rules(rules: str) -> str:
    """Terms hash for a venue that publishes prose rather than strikes.

    The whole settlement text, whitespace-normalised. An approval on the other
    venue is bound to the wording it was given for, exactly as it is on Kalshi,
    so a rewritten description suspends the pair rather than quietly changing
    what somebody signed for.
    """
    return hashlib.sha256(" ".join(rules.split()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CrossVenueCandidate:
    """Two contracts that might be the same claim."""

    kalshi_ticker: str
    kalshi_question: str
    kalshi_rules: str
    other_venue: str
    other_id: str
    other_question: str
    other_rules: str
    score: float  # money-path: allow -- ranks a review queue, prices nothing

    @property
    def slug(self) -> str:
        return f"xvenue:{self.kalshi_ticker}~{self.other_venue}:{self.other_id}"


def find_candidates(
    kalshi_markets: list[dict[str, Any]],
    other_markets: list[dict[str, Any]],
    *,
    # A ranking score, never a price. It orders a review queue; no cost, size
    # or edge is ever computed from it, and a pair it ranks first is worth
    # exactly what one it ranks last is worth until a person reads both texts.
    min_score: float = 0.55,  # money-path: allow -- ranks a queue, prices nothing
    limit: int = 200,
) -> list[CrossVenueCandidate]:
    """Rank possible pairs. Nothing here is a match; everything is a question.

    ``kalshi_markets`` are market-level payloads, not events. Kalshi lists a
    nomination as one event containing a market per candidate, while the other
    venue lists each candidate as its own question -- so comparing event titles
    pairs a container against a contract and produces nonsense. The first pass
    at this did exactly that and matched "Which party will win the 2032
    Presidential Election?" to eighteen individual 2028 candidates.
    """
    scored: list[CrossVenueCandidate] = []

    for km in kalshi_markets:
        title = f"{km.get('yes_sub_title', '')} {km.get('event_title', '')}".strip()
        if not title:
            continue
        for om in other_markets:
            question = str(om.get("question", ""))
            score = similarity(title, question)
            if score < min_score:
                continue
            scored.append(
                CrossVenueCandidate(
                    kalshi_ticker=str(km.get("ticker", "")),
                    kalshi_question=title,
                    kalshi_rules=str(km.get("rules_primary", "")),
                    other_venue=str(om.get("venue", "polymarket")),
                    other_id=str(om.get("market_id") or om.get("id") or ""),
                    other_question=question,
                    other_rules=str(om.get("rules") or om.get("description") or ""),
                    score=score,
                )
            )
    scored.sort(key=lambda c: -c.score)
    return scored[:limit]


def propose_pairs(session: Session, candidates: list[CrossVenueCandidate]) -> list[str]:
    """Draft each candidate as a ``PENDING`` cross-venue relationship.

    Returns the slugs drafted. Every one is unusable until a reviewer approves
    it, which is the only reason drafting from a similarity score is acceptable
    at all.
    """
    registry = RelationshipRegistry(session)
    drafted: list[str] = []

    for candidate in candidates:
        if registry.latest(candidate.slug) is not None:
            continue

        registry.draft(
            slug=candidate.slug,
            relationship_type=RelationshipType.CROSS_VENUE_PAIR,
            legs=[
                {
                    "venue": "kalshi",
                    "ticker": candidate.kalshi_ticker,
                    "side": "yes",
                    "ratio": 1,
                },
                {
                    "venue": candidate.other_venue,
                    "ticker": candidate.other_id,
                    "side": "no",
                    "ratio": 1,
                },
            ],
            payout_proof={
                "claim": (
                    "these two contracts settle on the same event, so YES on one and "
                    "NO on the other pays exactly $1.00 in every state"
                ),
                "similarity": round(candidate.score, 3),
                "similarity_is_not_evidence": (
                    "This score orders a review queue and establishes nothing. An "
                    "automated pass over these venues matched 'who will RUN FOR the "
                    "2028 Republican nomination' to 'will X WIN the 2028 Republican "
                    "nomination' at 0.86. Those are different events, the price gap "
                    "between them is large, and it reads exactly like an arbitrage."
                ),
                "kalshi_question": candidate.kalshi_question,
                "kalshi_settlement": candidate.kalshi_rules,
                "other_venue": candidate.other_venue,
                "other_question": candidate.other_question,
                "other_settlement": candidate.other_rules,
                "reviewer_must_confirm": [
                    "the two settlement texts resolve identically in EVERY state, not "
                    "merely describe the same subject",
                    "they resolve from the same source, or from sources that cannot disagree",
                    "they resolve at the same time, so one cannot pay out while the "
                    "other is still undecided",
                    "neither can void or refund independently of the other",
                ],
            },
            dependency_hashes={
                candidate.kalshi_ticker: normalize_kalshi_market(
                    {
                        "ticker": candidate.kalshi_ticker,
                        "rules_primary": candidate.kalshi_rules,
                    }
                ).terms_hash,
                f"{candidate.other_venue}:{candidate.other_id}": _hash_rules(candidate.other_rules),
            },
            notes=(
                f"cross-venue candidate at similarity {candidate.score:.2f}; "
                f"nothing has checked that these are the same claim"
            ),
        )
        drafted.append(candidate.slug)

    return drafted
