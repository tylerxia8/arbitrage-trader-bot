"""The falsification report (EPIC-13, FR-014).

Replays the archive through the real detector, shadow-executes whatever
qualifies, and books the results through the capital ledger. The output is a
**funnel**: of every basket pricing the archive supports, how many died at
each gate and why.

The funnel is the point. "Nothing qualified" is not a finding -- it is
compatible with a broken detector, an empty archive, a threshold set too
tight, or a strategy that does not work, and those need different responses.
Counting the gate each candidate died at distinguishes them. If everything
dies on staleness the instrument is wrong; if everything dies on net edge the
strategy is wrong; if everything dies on approval nobody has done the review.

Two deliberate choices about honesty:

**Research mode is labelled, not hidden.** Nothing here has an approved
relationship behind it, so a strict run rejects every candidate with
``relationship_not_approved`` -- correct, and useless as evidence. Research
mode prices structurally-discovered partitions as if approved, which answers
"would anything qualify if a reviewer signed" while stating plainly that
nobody has.

That distinction only became load-bearing once the fee rule was confirmed.
Before then ``research_mode`` had a single consequence -- whether an unverified
fee could price at all -- so a strict run died on ``unknown_fee`` and never
reached the approval gate. With the general taker rule now verified against the
venue's published schedule, a strict run that did not consult the registry
would report qualified candidates for leg sets nobody has signed for, which is
exactly the claim FR-005 exists to prevent. So it consults it.

**The staleness sweep is the honest way to report a polled archive.** The
detector's threshold is two seconds; polled books are 0-30 seconds old, so a
strict run rejects everything on quote age. Reporting only that would hide
whether an opportunity existed at all. Running the funnel at several
thresholds separates "there was nothing there" from "there was something and
we were too slow to see it" -- which are different findings with different
consequences.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.analysis.baskets import ConfirmationIndex, event_of
from arbbot.db.models import BookSnapshot
from arbbot.detector import BasketRequest, evaluate_basket
from arbbot.fees import KALSHI_SCHEDULE, FeeSchedule
from arbbot.ledger import CapitalLedger
from arbbot.marketdata.book import OrderBook
from arbbot.marketdata.types import BookSide, PriceLevel
from arbbot.money import PAYOUT_DOLLARS, ZERO
from arbbot.reasons import RejectionReason
from arbbot.registry import RelationshipRegistry
from arbbot.shadow import ShadowConfig, simulate_basket

__all__ = ["FalsificationReport", "StalenessSlice", "run_falsification"]


@dataclass(slots=True)
class StalenessSlice:
    """The funnel at one quote-age threshold."""

    max_age: dt.timedelta
    evaluated: int = 0
    accepted: int = 0
    rejections: Counter[str] = field(default_factory=Counter)

    shadow_attempted: int = 0
    shadow_completed: int = 0
    shadow_exposed: int = 0
    gross_edge: Decimal = ZERO
    realized: Decimal = ZERO
    unwind_losses: Decimal = ZERO

    @property
    def acceptance_rate(self) -> Decimal:
        if self.evaluated == 0:
            return ZERO
        return Decimal(self.accepted) / Decimal(self.evaluated)

    @property
    def dominant_reason(self) -> str:
        """The gate that killed the most candidates -- the binding constraint."""
        if not self.rejections:
            return "none"
        return self.rejections.most_common(1)[0][0]


@dataclass(slots=True)
class FalsificationReport:
    """What the archive says about the strategy."""

    slices: list[StalenessSlice] = field(default_factory=list)
    events_seen: int = 0
    snapshots_read: int = 0
    research_mode: bool = True
    window: tuple[dt.datetime | None, dt.datetime | None] = (None, None)

    confirmations_available: bool = False
    """Whether the archive records which markets each poll cycle confirmed.

    Decisive for reading the staleness column. Without it, quote age is
    measured from the last time a book *changed*, so a market that sat quiet
    under a live poller reads as minutes stale -- which is how this report's
    first verdict came to blame latency for what was a measurement artefact.
    """

    def render(self) -> str:
        lines: list[str] = []
        start, end = self.window
        if start and end:
            span = end - start
            lines.append(f"archive window   : {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}")
            lines.append(f"                   {span.total_seconds() / 86400:.2f} days")
        lines.append(f"snapshots read   : {self.snapshots_read:,}")
        lines.append(f"events           : {self.events_seen}")
        if self.confirmations_available:
            lines.append("quote age from   : last poll that confirmed the leg")
        else:
            lines.append("quote age from   : last CHANGE -- no poll-cycle record in this window.")
            lines.append("                   A book confirmed every second but unchanged for ten")
            lines.append("                   minutes reads as ten minutes stale. Any staleness")
            lines.append("                   figure below is an upper bound, badly so.")

        if self.research_mode:
            lines.append("")
            lines.append("RESEARCH MODE: relationships are priced as if approved. None are.")
            lines.append("No candidate below is tradeable, and no reviewer has signed for any")
            lines.append("of these leg sets being mutually exclusive and collectively exhaustive.")
        else:
            lines.append("")
            lines.append("STRICT MODE: only leg sets an approved relationship covers exactly")
            lines.append("were priced, and only on a fee rule confirmed against the venue's")
            lines.append("published schedule. Everything else is counted as rejected.")

        for slice_ in self.slices:
            age = slice_.max_age.total_seconds()
            lines.append("")
            lines.append(f"--- quote age allowed: {age:g}s " + "-" * 40)
            lines.append(f"  basket pricings evaluated : {slice_.evaluated:,}")
            lines.append(f"  qualified                 : {slice_.accepted:,}")
            if slice_.rejections:
                lines.append("  rejected by reason:")
                for reason, count in slice_.rejections.most_common():
                    share = count * 100 // max(slice_.evaluated, 1)
                    lines.append(f"    {reason:<28} {count:>7,}  ({share}%)")
            if slice_.shadow_attempted:
                lines.append(
                    f"  shadow-executed           : {slice_.shadow_attempted:,} attempted, "
                    f"{slice_.shadow_completed:,} completed, {slice_.shadow_exposed:,} left exposed"
                )
                lines.append(f"  gross edge on qualifying  : ${slice_.gross_edge:.2f}")
                lines.append(f"  realised after execution  : ${slice_.realized:.2f}")
                lines.append(f"  unwind losses             : ${slice_.unwind_losses:.2f}")

        lines.append("")
        lines.append("Fees are taker fees on the confirmed general rule, which is what")
        lines.append("assembling a basket costs: every leg crosses the spread. Shadow")
        lines.append("execution omits queue position, venue rejections, and market impact")
        lines.append("-- all of which make reality worse, so these figures are an upper bound.")
        return "\n".join(lines)


def _levels_from(levels: dict[str, str]) -> list[PriceLevel]:
    return [PriceLevel(Decimal(price), Decimal(size)) for price, size in levels.items()]


def _book_from(snapshot_yes: dict[str, str], snapshot_no: dict[str, str]) -> OrderBook:
    book = OrderBook("replay")
    entries: list[tuple[BookSide, PriceLevel]] = []
    entries += [(BookSide.YES, level) for level in _levels_from(snapshot_yes)]
    entries += [(BookSide.NO, level) for level in _levels_from(snapshot_no)]
    book.apply_snapshot(entries, sequence=1)
    return book


def run_falsification(
    session: Session,
    *,
    quantity: Decimal = Decimal("10"),
    min_net_edge: Decimal = ZERO,
    fees: FeeSchedule = KALSHI_SCHEDULE,
    staleness_thresholds: tuple[dt.timedelta, ...] = (
        dt.timedelta(seconds=2),
        dt.timedelta(seconds=30),
        dt.timedelta(seconds=90),
    ),
    shadow: ShadowConfig | None = None,
    research_mode: bool = True,
    starting_capital: Decimal = Decimal("1000"),
    since: dt.datetime | None = None,
) -> FalsificationReport:
    """Replay the archive and report where candidates die.

    ``research_mode`` prices structurally-discovered leg sets as though a
    reviewer had approved them. It is the only way to get a useful funnel
    before anyone has done the review, and the report says so loudly.
    """
    stmt = select(
        BookSnapshot.ticker,
        BookSnapshot.captured_ts,
        BookSnapshot.yes_levels,
        BookSnapshot.no_levels,
        BookSnapshot.is_complete,
    ).order_by(BookSnapshot.captured_ts, BookSnapshot.id)
    if since is not None:
        stmt = stmt.where(BookSnapshot.captured_ts >= since)
    rows = session.execute(stmt).all()

    legs_by_event: dict[str, set[str]] = {}
    for ticker, *_ in rows:
        legs_by_event.setdefault(event_of(ticker), set()).add(ticker)

    # Strict mode consults the registry for real. Until the fee rule was
    # confirmed, ``research_mode`` had only one consequence -- whether an
    # unverified fee could price -- so a strict run rejected everything on
    # ``unknown_fee`` and the approval gate was never exercised. With fees now
    # verified, a strict run that skipped this check would report qualified
    # candidates for leg sets nobody has signed for, which is precisely the
    # claim FR-005 exists to prevent.
    #
    # Keyed on the terms hashes because those record the exact legs the
    # reviewer read. A basket missing an outcome pays nothing when that outcome
    # occurs, so the approved set must match exactly, not merely contain.
    approved_leg_sets: set[frozenset[str]] = set()
    if not research_mode:
        approved_leg_sets = {
            frozenset(record.dependency_hashes)
            for record in RelationshipRegistry(session).approved()
        }

    report = FalsificationReport(
        events_seen=len(legs_by_event),
        snapshots_read=len(rows),
        research_mode=research_mode,
        window=(rows[0][1], rows[-1][1]) if rows else (None, None),
    )
    shadow_config = shadow or ShadowConfig()
    # Windowed with the rows: reading confirmations from outside the window
    # would credit a leg with a poll that no pricing here can see.
    confirmations = ConfirmationIndex(session, since=since)
    report.confirmations_available = bool(confirmations)

    for threshold in staleness_thresholds:
        slice_ = StalenessSlice(max_age=threshold)
        ledger = CapitalLedger()
        ledger.deposit(starting_capital, at=rows[0][1] if rows else dt.datetime.now(dt.UTC))

        latest: dict[str, tuple[OrderBook, dt.datetime]] = {}

        for ticker, captured, yes_levels, no_levels, is_complete in rows:
            if not is_complete:
                continue
            latest[ticker] = (_book_from(yes_levels or {}, no_levels or {}), captured)

            event = event_of(ticker)
            legs = legs_by_event[event]
            if len(legs) < 2 or not legs <= latest.keys():
                continue

            if not research_mode and frozenset(legs) not in approved_leg_sets:
                slice_.evaluated += 1
                slice_.rejections[str(RejectionReason.RELATIONSHIP_NOT_APPROVED)] += 1
                continue

            books = {leg: latest[leg][0] for leg in legs}
            # Measured from the last poll that *confirmed* each leg, not the
            # last one that changed it. The archive stores a snapshot only on
            # change, so a book quoted flat for ten minutes has a ten-minute-old
            # row and, under a live poller, a one-second-old observation.
            # Charging it the ten minutes is what produced this report's
            # original headline finding, and that finding was an artefact.
            ages = {
                leg: captured - confirmations.last_confirmed(leg, captured, default=latest[leg][1])
                for leg in legs
            }

            evaluation = evaluate_basket(
                BasketRequest(
                    books=books,
                    quantity=quantity,
                    fees=fees,
                    min_net_edge=min_net_edge,
                    book_ages=ages,
                    max_book_age=threshold,
                    require_verified_fees=not research_mode,
                ),
                now=captured,
            )
            slice_.evaluated += 1

            if not evaluation.accepted:
                slice_.rejections[str(evaluation.reason)] += 1
                continue

            slice_.accepted += 1
            slice_.gross_edge += evaluation.net_edge

            # Qualified on paper. Now find out what acquiring it would have done.
            ordered = [(leg, books[leg].ask_levels(BookSide.YES)) for leg in sorted(legs)]
            fill = simulate_basket(ordered, quantity, config=shadow_config)
            slice_.shadow_attempted += 1

            if fill.complete:
                slice_.shadow_completed += 1
                slice_.realized += (
                    PAYOUT_DOLLARS * quantity - fill.acquisition_cost - evaluation.fees
                )
            else:
                if fill.exposed:
                    slice_.shadow_exposed += 1
                slice_.unwind_losses += fill.unwind_loss
                slice_.realized -= fill.unwind_loss

        report.slices.append(slice_)

    return report
