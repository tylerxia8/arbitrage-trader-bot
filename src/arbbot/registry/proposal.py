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
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from arbbot.db.models import RelationshipRecord
from arbbot.normalize.terms import normalize_kalshi_market
from arbbot.registry.service import RegistryError, RelationshipRegistry
from arbbot.relationships import RelationshipStatus, RelationshipType
from arbbot.venues.kalshi.discovery import (
    CoverageReport,
    check_integer_coverage,
    classify_event,
)

__all__ = [
    "ProposalOutcome",
    "ProposalReport",
    "approve_group",
    "fingerprint_of",
    "group_pending",
    "pending",
    "propose_from_events",
    "review_fingerprint",
    "review_templates",
    "rules_template",
    "slug_for",
]


def slug_for(event_ticker: str) -> str:
    """Stable identity for a relationship across its versions."""
    return f"kalshi:{event_ticker}"


_DIGITS = re.compile(r"\d+")
#: Full and abbreviated month names. The venue writes both -- "August 13, 2026"
#: in one series and "Aug 14, 2026" in another -- and masking only the long form
#: leaves two instances of the same claim looking like different claims.
_MONTHS = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    r"(uary|ruary|ch|il|e|y|ust|tember|ober|ember)?\b",
    re.IGNORECASE,
)


def rules_template(text: str) -> str:
    """A settlement rule with its instance parameters masked out.

    ``"...in Dallas for August 14, 2026 ... is between 101-102°..."`` becomes
    ``"...in Dallas for <MONTH> #, # ... is between #-#°..."``.

    Masking is a judgement, so it is worth being exact about which one. Dates
    and strike numbers are the parameters that make one day's Dallas partition
    a different instance of the same claim; everything else -- the city, the
    reporting agency, the report name, whether it says highest or lowest -- is
    left alone, because those are what make it a *different* claim. "Highest
    temperature in Dallas" and "lowest temperature in Dallas" contain no
    differing digits and still mask apart, which is the property that matters.

    The masked text is stored on the draft, so a reviewer can see exactly what
    was treated as an instance parameter rather than taking it on trust.
    """
    collapsed = " ".join(text.split())
    return _DIGITS.sub("#", _MONTHS.sub("<MONTH>", collapsed))


def review_templates(markets: list[dict[str, Any]]) -> list[str]:
    """The distinct masked rules a reviewer would read for this event.

    Deduplicated: six temperature buckets produce three templates, because the
    four interior buckets differ only in their strike numbers and mask to the
    same sentence. That is the point -- it is one rule read three ways, not
    six.
    """
    return sorted({rules_template(str(m.get("rules_primary") or "")) for m in markets} - {""})


def review_fingerprint(markets: list[dict[str, Any]]) -> str:
    """Hash of what a reviewer actually reads, ignoring which day it is.

    A single proposal pass drafts one relationship per live event, and daily
    temperature markets rotate: the first live run produced eighty drafts, of
    which "Dallas high on the 14th" and "Dallas high on the 15th" differ only
    in expiry and strike numbers. Asking someone to read eighty separate
    settlement rules is how approval-by-fatigue happens, and a reviewer who has
    stopped reading is worse than no reviewer, because the record still says a
    person signed.

    So this fingerprints the *masked rules text, bucket shape and leg count*
    rather than the instance. Two events with the same fingerprint are the same
    claim asked about different days, and one reading answers both.

    It deliberately does **not** replace per-relationship approval. Each event
    still gets its own approval row, its own reviewer name, and its own binding
    to its own terms hashes -- this only lets one act of reading cover the set
    it genuinely covers. The guarantee is unchanged; the clerical work is not.
    """
    material = {
        "legs": len(markets),
        "strike_types": sorted(str(m.get("strike_type") or "") for m in markets),
        "templates": review_templates(markets),
        "settlement_sources": sorted({str(m.get("settlement_source") or "") for m in markets}),
    }
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
    event: dict[str, Any],
    markets: list[dict[str, Any]],
    coverage_summary: str,
    *,
    machine_checked: bool = True,
) -> dict[str, Any]:
    """The case the reviewer is being asked to accept, not just its conclusion."""
    return {
        "claim": (
            "exactly one leg settles YES, so holding one contract of every leg "
            "pays exactly $1.00 whatever the outcome"
        ),
        "review_fingerprint": review_fingerprint(markets),
        # The masked rules the fingerprint was taken over, so a reviewer can see
        # what was treated as an instance parameter rather than trust that it was
        # only the date and the strikes.
        "rules_templates": review_templates(markets),
        "event_title": event.get("title"),
        "structure": (
            "numeric buckets with both tails" if machine_checked else "exclusive named outcomes"
        ),
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
        "coverage_machine_checked": machine_checked,
        "reviewer_must_confirm": [
            (
                "the buckets leave no possible outcome unresolved and none overlapping"
                if machine_checked
                else "THAT NO POSSIBLE RESULT FALLS OUTSIDE THIS LIST -- nothing has "
                "checked this, and a set missing one outcome pays nothing when it occurs"
            ),
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

        if not structure.verdict.coverage_is_checkable:
            # A categorical set has no strikes to tile. Running the integer
            # check here and reporting "covered" would be a verification that
            # verified nothing, which is worse than none at all -- a reviewer
            # would read it as the machine having confirmed exhaustiveness.
            coverage = CoverageReport(
                covered=False,
                problems=(
                    "not machine-checkable: these are named outcomes, so whether they "
                    "exhaust the space is a question about the world and not about the "
                    "strikes. A reviewer must confirm no possible result falls outside "
                    "this list.",
                ),
            )
        else:
            coverage = check_integer_coverage(markets)
        if structure.verdict.coverage_is_checkable and not coverage.covered:
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

        proof = _payout_proof(
            event,
            markets,
            coverage.summary,
            machine_checked=structure.verdict.coverage_is_checkable,
        )
        existing = registry.latest(slug_for(ticker))
        if existing is not None and dict(existing.dependency_hashes) == hashes:
            detail = "leg set and terms match the latest version"
            if (
                existing.status is RelationshipStatus.PENDING
                and dict(existing.payout_proof) != proof
            ):
                # Refresh the case being presented, not the binding. The
                # dependency hashes are what an approval is bound to and they
                # have not moved; payout_proof is the evidence put in front of a
                # reviewer, and a draft nobody has signed should show the
                # current best version of it -- otherwise a draft written before
                # this system learned to fingerprint stays unreviewable forever.
                #
                # Deliberately never on an APPROVED record: rewriting what a
                # reviewer saw after they signed would forge the audit trail.
                existing.payout_proof = proof
                session.flush()
                detail = "unchanged; restated the case for review"
            report.outcomes.append(ProposalOutcome(ticker, "unchanged", detail, len(markets)))
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
            relationship_type=(
                RelationshipType.INTERVAL_PARTITION
                if structure.verdict.coverage_is_checkable
                else RelationshipType.EXHAUSTIVE_BASKET
            ),
            legs=_leg_definitions(markets),
            payout_proof=proof,
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

    A record with no fingerprint gets a group of its own rather than joining
    the other unfingerprinted ones. Unknown is not the same as same: the first
    version of this collapsed eighty drafts that had never recorded a
    fingerprint into a single row reading "80 events, 1 distinct claim", which
    is precisely the false reassurance this path exists to refuse. They are
    keyed on their own slug so they stay separate and stay visible.
    """
    groups: dict[str, list[RelationshipRecord]] = {}
    for record in pending(session):
        fingerprint = fingerprint_of(record)
        key = fingerprint or f"unfingerprinted:{record.slug}"
        groups.setdefault(key, []).append(record)
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
