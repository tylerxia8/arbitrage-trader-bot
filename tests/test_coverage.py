"""Collection coverage against the Milestone 1 exit gate.

The gate is "7 days continuous collection". These tests pin down what
"continuous" is allowed to mean, because every plausible weakening of it --
counting total samples, averaging, ignoring the tail -- lets a broken run
claim a week it did not have.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from arbbot.collection.coverage import GATE_DURATION, assess_coverage
from arbbot.db.models import FeedHealth

T0 = dt.datetime(2026, 8, 1, 0, 0, tzinfo=dt.UTC)
STREAM = "orderbook_poll:KXHIGHTATL"


def add_samples(
    session: Session,
    start: dt.datetime,
    duration: dt.timedelta,
    *,
    key: str = STREAM,
    every: dt.timedelta = dt.timedelta(minutes=1),
) -> None:
    at = start
    while at <= start + duration:
        session.add(
            FeedHealth(
                observed_ts=at,
                venue="kalshi",
                subscription_key=key,
                messages=1,
                gaps=0,
                missing_messages=0,
                duplicates=0,
                rewinds=0,
                reconnects=0,
                parse_errors=0,
                last_message_ts=at,
                lag_ms=10,
                is_healthy=True,
            )
        )
        at += every
    session.flush()


class TestGate:
    def test_an_empty_archive_cannot_meet_the_gate(self, session: Session) -> None:
        assessment = assess_coverage(session, now=T0)
        assert not assessment.meets_gate
        assert "empty archive" in assessment.render()

    def test_seven_unbroken_days_meets_the_gate(self, session: Session) -> None:
        add_samples(session, T0, dt.timedelta(days=7))
        assessment = assess_coverage(session, now=T0 + dt.timedelta(days=7))

        assert assessment.meets_gate
        assert assessment.longest_continuous >= GATE_DURATION

    def test_six_days_does_not(self, session: Session) -> None:
        add_samples(session, T0, dt.timedelta(days=6))
        assert not assess_coverage(session, now=T0 + dt.timedelta(days=6)).meets_gate


class TestGaps:
    def test_a_hole_splits_the_run_rather_than_averaging_it(self, session: Session) -> None:
        """Eight days of samples with a hole on day four is not eight days of
        continuous collection. It is two shorter runs, and the gate should say
        so -- this is the laptop-slept-overnight case."""
        add_samples(session, T0, dt.timedelta(days=4))
        add_samples(session, T0 + dt.timedelta(days=4, hours=6), dt.timedelta(days=4))

        assessment = assess_coverage(session, now=T0 + dt.timedelta(days=8, hours=6))
        stream = assessment.streams[0]

        assert stream.span > GATE_DURATION
        assert not assessment.meets_gate
        assert assessment.longest_continuous < dt.timedelta(days=5)

    def test_the_largest_gap_is_reported(self, session: Session) -> None:
        add_samples(session, T0, dt.timedelta(hours=1))
        add_samples(session, T0 + dt.timedelta(hours=13), dt.timedelta(hours=1))

        stream = assess_coverage(session, now=T0 + dt.timedelta(hours=14)).streams[0]
        assert stream.longest_gap >= dt.timedelta(hours=11)

    def test_brief_jitter_is_not_an_outage(self, session: Session) -> None:
        """Ordinary scheduling jitter must not be reported as a hole, or the
        report becomes noise nobody reads."""
        add_samples(session, T0, dt.timedelta(days=1), every=dt.timedelta(minutes=2))
        assert assess_coverage(session, now=T0 + dt.timedelta(days=1)).streams[0].gaps == []

    def test_current_silence_is_recorded_as_an_outage(self, session: Session) -> None:
        """Ongoing silence is a collector-level outage, so the report is
        honest that nothing is collecting right now."""
        add_samples(session, T0, dt.timedelta(days=2))
        assessment = assess_coverage(session, now=T0 + dt.timedelta(days=2, hours=3))
        assert assessment.collector_gaps

    def test_a_completed_week_survives_the_collector_stopping(self, session: Session) -> None:
        """Coverage measures the archive; /health measures now. A finished
        seven-day run does not un-happen because the collector was switched
        off -- which is exactly what you would do to go and analyse it."""
        add_samples(session, T0, dt.timedelta(days=7))
        assessment = assess_coverage(session, now=T0 + dt.timedelta(days=7, hours=3))

        assert assessment.collector_gaps, "the outage should still be visible"
        assert assessment.meets_gate, "seven continuous days were collected"

    def test_an_unfinished_run_is_not_rescued_by_stopping(self, session: Session) -> None:
        """The mirror case: four days then silence is still four days."""
        add_samples(session, T0, dt.timedelta(days=4))
        assessment = assess_coverage(session, now=T0 + dt.timedelta(days=9))
        assert not assessment.meets_gate


class TestRotatingMarkets:
    def test_daily_markets_can_still_meet_the_gate(self, session: Session) -> None:
        """The case that forced the gate to be rewritten.

        The recommended universe is daily temperature partitions: they exist
        for about a day and then settle. Requiring seven unbroken days *per
        stream* is unsatisfiable by construction, and a gate no correct run
        can pass is a bug, not a standard. Seven consecutive one-day markets,
        handed over cleanly, is exactly what seven days of continuous
        collection looks like here.
        """
        for day in range(7):
            add_samples(
                session,
                T0 + dt.timedelta(days=day),
                dt.timedelta(days=1),
                key=f"orderbook_poll:KXHIGHTATL-DAY{day}",
            )

        assessment = assess_coverage(session, now=T0 + dt.timedelta(days=7))
        assert len(assessment.streams) == 7
        assert assessment.meets_gate
        assert not assessment.interrupted_streams

    def test_a_market_ending_is_not_an_interruption(self, session: Session) -> None:
        """A stream stopping because its market settled is the market ending,
        not a hole in collection."""
        add_samples(session, T0, dt.timedelta(hours=20), key="orderbook_poll:DAY0")
        add_samples(session, T0 + dt.timedelta(hours=20), dt.timedelta(hours=20), key="op:DAY1")

        assessment = assess_coverage(session, now=T0 + dt.timedelta(hours=40))
        assert assessment.collector_gaps == []

    def test_a_handover_hole_is_an_outage(self, session: Session) -> None:
        """If nothing was being collected between one day's markets and the
        next, that window is genuinely unobserved."""
        add_samples(session, T0, dt.timedelta(hours=10), key="orderbook_poll:DAY0")
        add_samples(session, T0 + dt.timedelta(hours=16), dt.timedelta(hours=10), key="op:DAY1")

        assessment = assess_coverage(session, now=T0 + dt.timedelta(hours=26))
        assert assessment.collector_gaps
        assert not assessment.meets_gate

    def test_a_stream_interrupted_while_listed_is_flagged(self, session: Session) -> None:
        """Separate from the gate: a market that went quiet while still live
        costs a basket a leg, even on an otherwise continuous run."""
        # Same key either side of the hole: one market that went quiet and
        # came back while still listed.
        add_samples(session, T0, dt.timedelta(hours=2), key="orderbook_poll:AAA")
        add_samples(
            session, T0 + dt.timedelta(hours=8), dt.timedelta(hours=2), key="orderbook_poll:AAA"
        )
        add_samples(session, T0, dt.timedelta(hours=10), key="orderbook_poll:STEADY")

        assessment = assess_coverage(session, now=T0 + dt.timedelta(hours=10))
        interrupted = {s.subscription_key for s in assessment.interrupted_streams}
        assert "orderbook_poll:AAA" in interrupted
        assert "orderbook_poll:STEADY" not in interrupted
        # The collector never stopped, because STEADY covered the window.
        assert assessment.collector_gaps == []


class TestRendering:
    def test_report_states_the_verdict_plainly(self, session: Session) -> None:
        add_samples(session, T0, dt.timedelta(days=7))
        rendered = assess_coverage(session, now=T0 + dt.timedelta(days=7)).render()

        assert "exit gate:" in rendered
        assert "MET" in rendered

    def test_report_names_the_largest_outage(self, session: Session) -> None:
        add_samples(session, T0, dt.timedelta(hours=1))
        add_samples(session, T0 + dt.timedelta(hours=13), dt.timedelta(hours=1))

        rendered = assess_coverage(session, now=T0 + dt.timedelta(hours=14)).render()
        assert "largest collection outage" in rendered
        assert "NOT MET" in rendered
