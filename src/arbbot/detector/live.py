"""Pricing every registered basket as the books arrive (FR-003, EPIC-10).

Until now detection only happened in replay: the funnel was something a person
ran by hand over an archive, and the ``evaluation`` table existed in the schema
without a single writer. That has two costs. A decision made after the fact
against a reconstructed book is not the decision a live system would have made,
and "nothing qualified today" stays an assertion rather than a count.

This closes both. Every poll cycle prices every registered relationship whose
full leg set it just confirmed, and persists the outcome -- accepted or
rejected, with the reason code, the fee rule, the staleness threshold and the
per-leg depth walk it was decided under. Rejections are the point: a table of
acceptances is a list of successes with no denominator.

**Economics and permission are recorded separately, and that is deliberate.**
``accepted`` says the basket cleared depth, freshness, fees and net edge.
``tradeable`` says that *and* an approved relationship covered the leg set.
Collapsing them either way loses something real: with one boolean meaning
tradeability, an archive gathered while relationships sit in review records
nothing but ``relationship_not_approved`` and cannot say whether an edge went
past; with one boolean meaning economics, the table appears to describe
opportunities that nobody has signed for. Only ``tradeable`` may ever gate an
order.

**Freshness is measured, never assumed.** A leg is priced only if this cycle
confirmed it, and its age is the time since that confirmation. A leg the cycle
failed to poll makes its whole basket unpriceable rather than merely older --
the detector treats unknown age as stale, and this does not hand it a guess.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.db.models import Evaluation as EvaluationRow
from arbbot.db.models import RelationshipRecord
from arbbot.detector.basket import BasketRequest, evaluate_basket
from arbbot.fees import KALSHI_SCHEDULE, FeeSchedule
from arbbot.marketdata.book import OrderBook
from arbbot.money import ZERO
from arbbot.relationships import RelationshipStatus

__all__ = ["DetectionReport", "LiveDetector"]


@dataclass(slots=True)
class DetectionReport:
    """What one cycle's detection pass decided."""

    priced: int = 0
    accepted: int = 0
    tradeable: int = 0
    skipped_incomplete: int = 0
    """Relationships whose full leg set this cycle did not confirm."""

    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    @property
    def dominant_reason(self) -> str:
        if not self.reasons:
            return "none"
        return max(self.reasons.items(), key=lambda kv: kv[1])[0]


class LiveDetector:
    """Prices registered relationships against the books a cycle just confirmed."""

    def __init__(
        self,
        *,
        quantity: Decimal = Decimal("10"),
        min_net_edge: Decimal = ZERO,
        max_book_age: dt.timedelta = dt.timedelta(seconds=2),
        fees: FeeSchedule = KALSHI_SCHEDULE,
    ) -> None:
        if quantity <= ZERO:
            raise ValueError("quantity must be positive")
        self.quantity = quantity
        self.min_net_edge = min_net_edge
        self.max_book_age = max_book_age
        self.fees = fees

    def _registered(self, session: Session) -> list[RelationshipRecord]:
        """Every relationship worth pricing.

        Pending ones included on purpose. They cannot be traded and are never
        marked tradeable, but pricing them is how the archive answers "was
        there an edge while this sat in review" -- which is exactly the
        question a reviewer needs answered to know whether reviewing is worth
        their time.
        """
        return list(
            session.execute(
                select(RelationshipRecord).where(
                    RelationshipRecord.status.in_(
                        (RelationshipStatus.APPROVED, RelationshipStatus.PENDING)
                    )
                )
            ).scalars()
        )

    def evaluate_cycle(
        self,
        session: Session,
        *,
        books: dict[str, OrderBook],
        confirmed_ts: dict[str, dt.datetime],
        now: dt.datetime,
    ) -> DetectionReport:
        """Price every registered basket this cycle can see, and persist each decision.

        :param books: reconstructed books by ticker, from the collectors.
        :param confirmed_ts: when this cycle last saw each ticker. A ticker
            absent here was not confirmed, and its baskets are not priced --
            guessing an age would defeat the freshness gate entirely.
        """
        report = DetectionReport()

        for record in self._registered(session):
            legs = sorted(record.dependency_hashes)
            if not legs or not set(legs) <= books.keys() or not set(legs) <= confirmed_ts.keys():
                report.skipped_incomplete += 1
                continue

            evaluation = evaluate_basket(
                BasketRequest(
                    books={leg: books[leg] for leg in legs},
                    quantity=self.quantity,
                    fees=self.fees,
                    min_net_edge=self.min_net_edge,
                    book_ages={leg: now - confirmed_ts[leg] for leg in legs},
                    max_book_age=self.max_book_age,
                    require_verified_fees=True,
                ),
                now=now,
            )

            approved = record.status is RelationshipStatus.APPROVED
            tradeable = evaluation.accepted and approved

            report.priced += 1
            report.accepted += evaluation.accepted
            report.tradeable += tradeable
            if not evaluation.accepted:
                report.note(str(evaluation.reason))

            session.add(
                EvaluationRow(
                    evaluated_ts=now,
                    relationship_id=record.id,
                    relationship_slug=record.slug,
                    relationship_version=record.version,
                    relationship_status=record.status.value,
                    accepted=evaluation.accepted,
                    tradeable=tradeable,
                    reason=str(evaluation.reason) if evaluation.reason else None,
                    detail=evaluation.detail or None,
                    quantity=evaluation.quantity,
                    acquisition_cost=evaluation.acquisition_cost,
                    fees=evaluation.fees,
                    reserves=evaluation.reserves,
                    guaranteed_payout=evaluation.guaranteed_payout,
                    net_edge=evaluation.net_edge,
                    # The depth walk, not just the total. A cost without the
                    # levels it came from cannot be re-derived, and NFR-03
                    # requires a past decision to reproduce from its own record.
                    legs=[
                        {
                            "ticker": leg.ticker,
                            "side": leg.side.value,
                            "requested": str(leg.walk.requested),
                            "filled": str(leg.walk.filled),
                            "cost": str(leg.walk.cost),
                            "levels_used": leg.walk.levels_used,
                            "worst_price": (
                                str(leg.walk.worst_price)
                                if leg.walk.worst_price is not None
                                else None
                            ),
                            "fee": str(leg.fee),
                        }
                        for leg in evaluation.legs
                    ],
                    fee_rule=self.fees.rule_for(legs[0]).name,
                    parser_version="kalshi-fp-v1",
                    max_book_age_ms=int(self.max_book_age.total_seconds() * 1000),
                )
            )

        return report
