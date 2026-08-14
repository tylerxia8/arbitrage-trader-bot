"""Turning structural discovery into something a person can sign for (FR-005).

Strict evaluation now refuses any leg set no approved relationship covers,
which is correct and leaves the system unable to qualify anything at all until
a review has happened. This is the path from "the venue lists six temperature
buckets that appear to tile the integers" to "a named human read the settlement
terms and accepted that claim".

The line this module exists to hold is that **proposing is not approving**.
Everything here is automatic: fetch the live events, classify their structure,
check the buckets leave no integer unresolved, hash each leg's settlement
terms, and write a ``PENDING`` record. None of it establishes anything. A
pending relationship cannot qualify a candidate, and the only thing that
changes that is :meth:`~arbbot.registry.service.RelationshipRegistry.approve`
with a reviewer's name and the evidence they read.

Two consequences worth stating.

**The proposal carries its own case.** ``payout_proof`` records the coverage
check, the bucket boundaries, and the settlement wording each leg was hashed
from -- so a reviewer is deciding on evidence rather than on this system's
say-so. A proposal that just asserted "these six are exhaustive" would be
asking for a rubber stamp.

**Re-proposing an unchanged event is a no-op.** A daily run would otherwise
draft a new version of every partition every time, burying real changes in a
pile of identical drafts and inviting approval-by-fatigue. A new version is
drafted only when the leg set or a leg's terms hash actually moves -- which is
also precisely the event that should suspend an existing approval.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from arbbot.db.models import RelationshipRecord
from arbbot.normalize.terms import normalize_kalshi_market
from arbbot.registry.service import RegistryError, RelationshipRegistry
from arbbot.relationships import RelationshipStatus, RelationshipType
from arbbot.venues.kalshi.discovery import check_integer_coverage, classify_event

__all__ = [
    "ProposalOutcome",
    "ProposalReport",
    "approve_group",
    "fingerprint_of",
    "group_pending",
    "pending",
    "propose_from_events",
    "review_fingerprint",
    "slug_for",
]


def slug_for(event_ticker: str) -> str:
    """Stable identity for a relationship across its versions."""
    return f"kalshi:{event_ticker}"


def review_fingerprint(markets: list[dict[str, Any]]) -> str:
    """Hash of what a reviewer actually reads, ignoring which day it is.

    A single proposal pass drafts one relationship per live event, and daily
    temperature markets rotate: eighty-odd drafts, of which "Dallas high on the
    14th" and "Dallas high on the 15th" differ only in expiry and strike
    numbers. Asking someone to read eighty separate settlement rules is how
    approval-by-fatigue happens, and a reviewer who stops reading is worse than
    no reviewer, because the record still says a person signed.

    So this fingerprints the *rules text and bucket shape* rather than the
    instance. Two events with the same fingerprint are the same claim asked
    about different days, and one reading answers both.

    It deliberately does **not** replace per-relationship approval. Each event
    still gets its own approval row, its own reviewer name, and its own binding
    to its own terms hashes -- this only lets one act of reading cover the set
    it genuinely covers. The guarantee is unchanged; the clerical work is not.
    """
    material = [
        {
            "strike_type": str(m.get("strike_type") or ""),
            "rules_primary": " ".join(str(m.get("rules_primary") or "").split()),
            "settlement_source": str(m.get("settlement_source") or ""),
        }
        for m in markets
    ]
    material.sort(key=lambda entry: (entry["strike_type"], entry["rules_primary"]))
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What happened to one event."""

    event_ticker: str
    action: str
    """``drafted``, ``unchanged``, ``suspended``, or ``skipped``."""

    detail: str
    legs: int = 0
    version: int | None = None


@dataclass(slots=True)
class ProposalReport:
    """Everything a proposal pass did, and what still needs a human."""

    outcomes: list[ProposalOutcome] = field(default_factory=list)
    events_seen: int = 0

    @property
    def drafted(self) -> list[ProposalOutcome]:
        return [o for o in self.outcomes if o.action == "drafted"]

    @property
    def suspended(self) -> list[ProposalOutcome]:
        return [o for o in self.outcomes if o.action == "suspended"]

    def render(self) -> str:
        lines = [
            f"events examined  : {self.events_seen}",
            f"drafted          : {len(self.drafted)}",
            f"suspended        : {len(self.suspended)}",
            f"unchanged        : {sum(1 for o in self.outcomes if o.action == 'unchanged')}",
            f"skipped          : {sum(1 for o in self.outcomes if o.action == 'skipped')}",
        ]
        interesting = [o for o in self.outcomes if o.action in ("drafted", "suspended")]
        if interesting:
            lines.append("")
            lines.append(f"{'event':<26} {'action':<10} {'legs':>4}  detail")
            lines.append("-" * 88)
            for outcome in interesting:
                lines.append(
                    f"{outcome.event_ticker:<26} {outcome.action:<10} "
                    f"{outcome.legs:>4}  {outcome.detail}"
                )

        lines.append("")
        if self.drafted:
            lines.append("Nothing above is approved. A draft is a request for review, and until")
            lines.append("a reviewer signs for one it cannot qualify any candidate. Approve with:")
            lines.append("  arbbot relationships approve <slug> --reviewer NAME --evidence TEXT")
        else:
            lines.append("No new drafts. Existing approvals are unaffected.")
        return "\n".join(lines)


def _leg_definitions(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per leg, in the shape the registry stores.

    Side and ratio are explicit rather than implied. An exhaustive basket is
    bought YES on every leg at equal size, and leaving that to be inferred
    downstream is how a relationship silently comes to mean something else.
    """
    return [
        {
            "venue": "kalshi",
            "ticker": str(market["ticker"]),
            "side": "yes",
            "ratio": 1,
            "floor_strike": market.get("floor_strike"),
            "cap_strike": market.get("cap_strike"),
            "strike_type": market.get("strike_type"),
        }
        for market in markets
    ]


def _payout_proof(
    event: dict[str, Any], markets: list[dict[str, Any]], coverage_summary: str
) -> dict[str, Any]:
    """The case the reviewer is being asked to accept, not just its conclusion."""
    return {
        "claim": (
            "exactly one leg settles YES, so holding one contract of every leg "
            "pays exactly $1.00 whatever the outcome"
        ),
        "review_fingerprint": review_fingerprint(markets),
        "event_title": event.get("title"),
        "structure": "numeric buckets with both tails",
        "integer_coverage": coverage_summary,
        "boundaries": [
            {
                "ticker": m.get("ticker"),
                "strike_type": m.get("strike_type"),
                "floor_strike": m.get("floor_strike"),
                "cap_strike": m.get("cap_strike"),
                "rules_primary": m.get("rules_primary"),
            }
            for m in markets
        ],
        "reviewer_must_confirm": [
            "the buckets leave no possible outcome unresolved and none overlapping",
            "every leg settles from the same source, report and time",
            "no leg can settle YES alongside another",
        ],
    }


def propose_from_events(
    session: Session,
    events: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    now: dt.datetime | None = None,
) -> ProposalReport:
    """Draft a ``PENDING`` relationship for every structurally-eligible event.

    ``events`` pairs each event payload with its tradeable markets. Taking them
    as an argument rather than fetching keeps this replayable and testable
    against recorded payloads.
    """
    registry = RelationshipRegistry(session)
    report = ProposalReport(events_seen=len(events))

    for event, markets in events:
        structure = classify_event(event, markets)
        ticker = structure.event_ticker

        if not structure.may_propose:
            report.outcomes.append(
                ProposalOutcome(ticker, "skipped", structure.reason, len(markets))
            )
            continue

        coverage = check_integer_coverage(markets)
        if not coverage.covered:
            # A set with a hole is not a basket, and drafting it would put a
            # reviewer in the position of approving something this system
            # already knows is broken.
            report.outcomes.append(
                ProposalOutcome(ticker, "skipped", coverage.summary, len(markets))
            )
            continue

        hashes = {
            str(market["ticker"]): normalize_kalshi_market(market).terms_hash for market in markets
        }

        existing = registry.latest(slug_for(ticker))
        if existing is not None and dict(existing.dependency_hashes) == hashes:
            report.outcomes.append(
                ProposalOutcome(
                    ticker, "unchanged", "leg set and terms match the latest version", len(markets)
                )
            )
            continue

        if existing is not None and existing.status is RelationshipStatus.APPROVED:
            # The approval was given for wording that no longer applies. FR-004
            # requires this to be automatic: the window between a terms change
            # and someone noticing is exactly when a basket stops being one.
            registry.suspend(
                existing, why="legs or settlement terms changed; superseded by a new draft"
            )
            report.outcomes.append(
                ProposalOutcome(
                    ticker,
                    "suspended",
                    "approved version no longer matches the venue; re-review needed",
                    len(markets),
                    existing.version,
                )
            )

        record = registry.draft(
            slug=slug_for(ticker),
            relationship_type=RelationshipType.INTERVAL_PARTITION,
            legs=_leg_definitions(markets),
            payout_proof=_payout_proof(event, markets, coverage.summary),
            dependency_hashes=hashes,
            notes=f"proposed from venue structure at {(now or dt.datetime.now(dt.UTC)):%Y-%m-%d %H:%M}Z",
        )
        report.outcomes.append(
            ProposalOutcome(
                ticker,
                "drafted",
                f"{coverage.summary}; awaiting review",
                len(markets),
                record.version,
            )
        )

    return report


def pending(session: Session) -> list[RelationshipRecord]:
    """Every relationship waiting on a reviewer."""
    return [
        record
        for record in session.query(RelationshipRecord)
        .filter(RelationshipRecord.status == RelationshipStatus.PENDING)
        .order_by(RelationshipRecord.slug, RelationshipRecord.version)
        .all()
    ]


def fingerprint_of(record: RelationshipRecord) -> str:
    """The review fingerprint a drafted relationship was stamped with."""
    return str(dict(record.payout_proof).get("review_fingerprint", ""))


def group_pending(session: Session) -> dict[str, list[RelationshipRecord]]:
    """Pending relationships grouped by what a reviewer would have to read.

    Records drafted before fingerprints existed group under the empty string
    and are therefore never merged with anything -- an unfingerprinted draft
    makes no claim about resembling another, and guessing that it does would be
    the one shortcut this whole path exists to refuse.
    """
    groups: dict[str, list[RelationshipRecord]] = {}
    for record in pending(session):
        groups.setdefault(fingerprint_of(record), []).append(record)
    return groups


def approve_group(
    session: Session,
    fingerprint: str,
    *,
    reviewer: str,
    evidence: str,
) -> list[RelationshipRecord]:
    """Approve every pending relationship a single reading genuinely covers.

    Each one still gets its own approval row, naming the reviewer and the
    evidence, and stays bound to its own legs' terms hashes -- so a later
    change to any one of them suspends that one alone. This is one act of
    reading applied to the set it covers, not one approval standing in for
    many.

    Refuses an empty fingerprint, which would otherwise sweep up every
    unfingerprinted draft in the registry under a single signature.
    """
    if not fingerprint:
        raise RegistryError(
            "refusing to approve on an empty review fingerprint: it would cover "
            "every draft that never recorded what a reviewer would have to read"
        )

    registry = RelationshipRegistry(session)
    matching = [r for r in pending(session) if fingerprint_of(r) == fingerprint]
    for record in matching:
        registry.approve(record, reviewer=reviewer, evidence=evidence)
    return matching
