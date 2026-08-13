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

    @property
    def meets_gate(self) -> bool:
        """Every stream must clear the gate, not the best one.

        A basket needs all its legs. Seven unbroken days on five markets and
        four days on the sixth does not evidence a basket -- it evidences five
        legs of one.
        """
        return bool(self.streams) and all(s.meets_gate for s in self.streams)

    @property
    def shortest_continuous(self) -> dt.timedelta:
        return min((s.longest_continuous for s in self.streams), default=dt.timedelta(0))

    def render(self) -> str:
        if not self.streams:
            return "no collection recorded: the gate cannot be met by an empty archive"

        lines = [
            f"{'stream':<44} {'span':>8} {'unbroken':>9} {'gaps':>5}  gate",
            "-" * 80,
        ]
        for stream in sorted(self.streams, key=lambda s: s.longest_continuous):
            lines.append(
                f"{stream.subscription_key:<44} "
                f"{stream.span.days:>6}d "
                f"{stream.longest_continuous.total_seconds() / 86400:>8.2f}d "
                f"{len(stream.gaps):>5}  "
                f"{'PASS' if stream.meets_gate else 'no'}"
            )

        worst = max(
            (g for s in self.streams for g in s.gaps),
            key=lambda g: g.duration,
            default=None,
        )
        lines.append("")
        lines.append(
            f"shortest unbroken stretch: "
            f"{self.shortest_continuous.total_seconds() / 86400:.2f} days "
            f"of the {GATE_DURATION.days} required"
        )
        if worst is not None:
            lines.append(f"largest outage:            {worst}")
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

    streams: list[StreamCoverage] = []
    for key, stamps in by_stream.items():
        gaps = [
            CoverageGap(previous, following)
            for previous, following in pairwise(stamps)
            if following - previous > gap_threshold
        ]
        # Silence since the last sample is an ongoing outage, not a tidy end,
        # so it is recorded. It does not retract the stretch before it: a
        # completed week stays completed once the collector is stopped.
        if stamps and at - stamps[-1] > gap_threshold:
            gaps.append(CoverageGap(stamps[-1], at))

        streams.append(
            StreamCoverage(
                subscription_key=key,
                first_sample=stamps[0] if stamps else None,
                last_sample=at if stamps and at - stamps[-1] > gap_threshold else stamps[-1],
                samples=len(stamps),
                gaps=gaps,
            )
        )

    return CoverageAssessment(streams=streams, assessed_ts=at)
