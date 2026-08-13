"""Collection coverage -- does the archive actually meet the exit gate?

Milestone 1 exits on "7 days continuous collection; replay works". Nothing
else in the system answers that. The collector reports health *now*, and the
archive holds everything that arrived, but neither says whether the week was
continuous or whether it has a six-hour hole in the middle where a laptop
slept.

The measurement is deliberately unflattering. It reports the **largest** gap
rather than an average or a percentage, because a week that is 99% covered
with one twelve-hour hole is not six and a half days of evidence -- it is two
shorter runs, and any claim about how often something occurs is weakened by
exactly the window nobody was watching.

Coverage measures the *archive*; ``/health`` measures *now*. A finished
seven-day run does not un-happen because the collector was switched off --
which is exactly what you would do in order to go and analyse it. The outage
is still reported, but the week it followed still counts.

Gaps are computed from ``feed_health`` samples rather than from archived
messages. A quiet market legitimately archives nothing for long stretches
(unchanged books are not re-stored), so absence of messages is not absence of
collection. Health samples are written on a timer whether or not anything
happened, which makes them the honest liveness record.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.collection.health import utc_now
from arbbot.db.models import FeedHealth

__all__ = ["GATE_DURATION", "CoverageGap", "StreamCoverage", "assess_coverage"]

#: The Milestone 1 exit gate: seven days of continuous collection.
GATE_DURATION = dt.timedelta(days=7)

#: A stretch longer than this between health samples counts as an outage.
#: Generous relative to the default 30-second sampling interval, so ordinary
#: scheduling jitter and a slow cycle do not register as holes.
DEFAULT_GAP_THRESHOLD = dt.timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """A stretch where a stream produced no health sample."""

    started_ts: dt.datetime
    ended_ts: dt.datetime

    @property
    def duration(self) -> dt.timedelta:
        return self.ended_ts - self.started_ts

    def __str__(self) -> str:
        hours = self.duration.total_seconds() / 3600
        return f"{hours:.1f}h from {self.started_ts:%Y-%m-%d %H:%M} to {self.ended_ts:%H:%M}"


@dataclass(slots=True)
class StreamCoverage:
    """How much continuous evidence one stream actually has."""

    subscription_key: str
    first_sample: dt.datetime | None
    last_sample: dt.datetime | None
    samples: int
    gaps: list[CoverageGap] = field(default_factory=list)

    @property
    def span(self) -> dt.timedelta:
        if self.first_sample is None or self.last_sample is None:
            return dt.timedelta(0)
        return self.last_sample - self.first_sample

    @property
    def longest_gap(self) -> dt.timedelta:
        return max((g.duration for g in self.gaps), default=dt.timedelta(0))

    @property
    def longest_continuous(self) -> dt.timedelta:
        """The longest unbroken stretch -- the number the gate cares about.

        Seven days of samples with a hole in the middle is not seven days of
        continuous collection. It is the longer of the two pieces.
        """
        if self.first_sample is None or self.last_sample is None:
            return dt.timedelta(0)

        boundaries = [self.first_sample]
        for gap in sorted(self.gaps, key=lambda g: g.started_ts):
            boundaries.extend([gap.started_ts, gap.ended_ts])
        boundaries.append(self.last_sample)

        return max(
            (boundaries[i + 1] - boundaries[i] for i in range(0, len(boundaries) - 1, 2)),
            default=dt.timedelta(0),
        )

    @property
    def meets_gate(self) -> bool:
        return self.longest_continuous >= GATE_DURATION


@dataclass(slots=True)
class CoverageAssessment:
    """The whole deployment's standing against the exit gate."""

    streams: list[StreamCoverage]
    assessed_ts: dt.datetime
    collector_gaps: list[CoverageGap] = field(default_factory=list)
    first_sample: dt.datetime | None = None
    last_sample: dt.datetime | None = None

    @property
    def longest_continuous(self) -> dt.timedelta:
        """Longest stretch during which *something* was being collected.

        This, not per-stream continuity, is what the gate measures -- and the
        distinction is forced by the markets themselves. The recommended
        universe is daily temperature partitions, which exist for about a day
        and then settle: yesterday's Atlanta event already has zero active
        markets. Requiring seven unbroken days per stream would be
        unsatisfiable by construction, and a gate no correct run can pass is
        not a gate, it is a bug.

        What seven days of continuous collection means for rotating markets is
        that the collector was alive and gathering whatever was live, without
        a hole where nobody was watching.
        """
        if self.first_sample is None or self.last_sample is None:
            return dt.timedelta(0)

        boundaries = [self.first_sample]
        for gap in sorted(self.collector_gaps, key=lambda g: g.started_ts):
            boundaries.extend([gap.started_ts, gap.ended_ts])
        boundaries.append(self.last_sample)

        return max(
            (boundaries[i + 1] - boundaries[i] for i in range(0, len(boundaries) - 1, 2)),
            default=dt.timedelta(0),
        )

    @property
    def meets_gate(self) -> bool:
        return bool(self.streams) and self.longest_continuous >= GATE_DURATION

    @property
    def interrupted_streams(self) -> list[StreamCoverage]:
        """Streams that went quiet *during their own lifetime*.

        A market ending is not an interruption; it is the market ending. A
        market that stopped reporting while still listed is a real hole, and
        this is where a basket loses a leg.
        """
        return [s for s in self.streams if s.gaps]

    def render(self) -> str:
        if not self.streams:
            return "no collection recorded: the gate cannot be met by an empty archive"

        days = self.longest_continuous.total_seconds() / 86400
        lines = [
            f"streams observed:          {len(self.streams)}",
            f"continuous collection:     {days:.2f} days of the {GATE_DURATION.days} required",
        ]

        worst = max(self.collector_gaps, key=lambda g: g.duration, default=None)
        if worst is not None:
            lines.append(f"largest collection outage: {worst}")
            lines.append(f"outages:                   {len(self.collector_gaps)}")
        else:
            lines.append("largest collection outage: none")

        interrupted = self.interrupted_streams
        if interrupted:
            # Reported separately from the gate: a hole in one market's stream
            # while it was still listed costs a basket a leg, even on a run
            # that was otherwise continuous.
            lines.append("")
            lines.append(f"streams interrupted while listed: {len(interrupted)}")
            for stream in sorted(interrupted, key=lambda s: -s.longest_gap)[:5]:
                lines.append(f"  {stream.subscription_key:<44} worst {stream.gaps[0]}")

        lines.append("")
        lines.append(f"exit gate:                 {'MET' if self.meets_gate else 'NOT MET'}")
        return "\n".join(lines)


def assess_coverage(
    session: Session,
    *,
    now: dt.datetime | None = None,
    gap_threshold: dt.timedelta = DEFAULT_GAP_THRESHOLD,
) -> CoverageAssessment:
    """Measure continuous collection per stream from the health record."""
    at = now or utc_now()
    rows = session.execute(
        select(FeedHealth.subscription_key, FeedHealth.observed_ts).order_by(
            FeedHealth.subscription_key, FeedHealth.observed_ts
        )
    ).all()

    by_stream: dict[str, list[dt.datetime]] = {}
    for key, observed in rows:
        stamped = observed if observed.tzinfo else observed.replace(tzinfo=dt.UTC)
        by_stream.setdefault(key, []).append(stamped)

    # Collector liveness is the union across streams: while any stream was
    # sampling, collection was happening. Individual markets rotate daily, so
    # only the union can be continuous across a week.
    all_stamps = sorted({stamp for stamps in by_stream.values() for stamp in stamps})
    collector_gaps = [
        CoverageGap(previous, following)
        for previous, following in pairwise(all_stamps)
        if following - previous > gap_threshold
    ]
    if all_stamps and at - all_stamps[-1] > gap_threshold:
        collector_gaps.append(CoverageGap(all_stamps[-1], at))

    streams: list[StreamCoverage] = []
    for key, stamps in by_stream.items():
        # Internal gaps only. Silence *after* a stream's last sample is not an
        # interruption -- for a daily market it is simply the market settling,
        # and counting it would flag every expired contract as a hole. Whether
        # anything is collecting *now* is a property of the collector, not of
        # one market, and is measured on the union above.
        streams.append(
            StreamCoverage(
                subscription_key=key,
                first_sample=stamps[0] if stamps else None,
                last_sample=stamps[-1] if stamps else None,
                samples=len(stamps),
                gaps=[
                    CoverageGap(previous, following)
                    for previous, following in pairwise(stamps)
                    if following - previous > gap_threshold
                ],
            )
        )

    return CoverageAssessment(
        streams=streams,
        assessed_ts=at,
        collector_gaps=collector_gaps,
        first_sample=all_stamps[0] if all_stamps else None,
        last_sample=(
            at
            if all_stamps and at - all_stamps[-1] > gap_threshold
            else (all_stamps[-1] if all_stamps else None)
        ),
    )
