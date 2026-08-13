"""The falsification funnel (EPIC-13, FR-014).

The funnel exists because "nothing qualified" is not a finding. It is equally
consistent with a broken detector, an empty archive, a threshold set too
tight, and a strategy that does not work -- and those need different
responses. These tests check that the report actually distinguishes them.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from arbbot.analysis.falsification import run_falsification
from arbbot.db.models import BookSnapshot
from arbbot.reasons import RejectionReason

D = Decimal
T0 = dt.datetime(2026, 8, 13, 12, 0, tzinfo=dt.UTC)
EVENT = "KXHIGHTEST-26AUG13"


def snap(
    session: Session,
    leg: str,
    *,
    yes_ask: str,
    size: str = "1000",
    at: dt.datetime = T0,
) -> None:
    """Store a snapshot whose implied YES ask is ``yes_ask``."""
    session.add(
        BookSnapshot(
            venue="kalshi",
            ticker=f"{EVENT}-{leg}",
            captured_ts=at,
            sequence=1,
            yes_levels={},
            no_levels={f"{D('1.00') - D(yes_ask):.4f}": size},
            checksum="x" * 64,
            is_complete=True,
        )
    )
    session.flush()


def cheap_basket(session: Session, at: dt.datetime = T0, ask: str = "0.20") -> None:
    for leg in ("A", "B", "C"):
        snap(session, leg, yes_ask=ask, at=at)


class TestFunnel:
    def test_an_empty_archive_reports_nothing(self, session: Session) -> None:
        report = run_falsification(session)
        assert report.snapshots_read == 0
        assert all(s.evaluated == 0 for s in report.slices)

    def test_a_cheap_basket_qualifies_and_is_counted(self, session: Session) -> None:
        cheap_basket(session)
        report = run_falsification(session, quantity=D("10"))
        fresh = report.slices[0]
        assert fresh.evaluated >= 1
        assert fresh.accepted >= 1

    def test_a_dear_basket_dies_on_net_edge(self, session: Session) -> None:
        """If everything dies here, the strategy is wrong."""
        cheap_basket(session, ask="0.40")
        report = run_falsification(session, quantity=D("10"))
        fresh = report.slices[0]
        assert fresh.accepted == 0
        assert fresh.dominant_reason == str(RejectionReason.NONPOSITIVE_NET_EDGE)

    def test_a_thin_basket_dies_on_depth(self, session: Session) -> None:
        for leg in ("A", "B", "C"):
            snap(session, leg, yes_ask="0.20", size="2")
        report = run_falsification(session, quantity=D("100"))
        assert report.slices[0].dominant_reason == str(RejectionReason.INSUFFICIENT_DEPTH)

    def test_the_dominant_reason_names_the_binding_constraint(self, session: Session) -> None:
        cheap_basket(session, ask="0.40")
        report = run_falsification(session, quantity=D("10"))
        assert report.slices[0].dominant_reason != "none"


class TestStalenessSweep:
    def test_stale_legs_die_at_a_tight_threshold_and_survive_a_loose_one(
        self, session: Session
    ) -> None:
        """The point of the sweep: it separates "there was nothing there" from
        "there was something and we were too slow to see it"."""
        snap(session, "A", yes_ask="0.20", at=T0)
        snap(session, "B", yes_ask="0.20", at=T0)
        snap(session, "C", yes_ask="0.20", at=T0 + dt.timedelta(seconds=45))

        report = run_falsification(
            session,
            quantity=D("10"),
            staleness_thresholds=(dt.timedelta(seconds=2), dt.timedelta(seconds=90)),
        )
        tight, loose = report.slices

        assert tight.accepted == 0
        assert tight.dominant_reason == str(RejectionReason.STALE_QUOTE)
        assert loose.accepted >= 1

    def test_every_threshold_is_reported(self, session: Session) -> None:
        cheap_basket(session)
        thresholds = (dt.timedelta(seconds=1), dt.timedelta(seconds=10), dt.timedelta(minutes=5))
        report = run_falsification(session, staleness_thresholds=thresholds)
        assert [s.max_age for s in report.slices] == list(thresholds)


class TestStrictMode:
    def test_strict_mode_refuses_unverified_fees(self, session: Session) -> None:
        """A run that will not price on an unconfirmed fee rule is the honest
        default for anything claiming to be tradeable."""
        cheap_basket(session)
        report = run_falsification(session, quantity=D("10"), research_mode=False)

        assert report.slices[0].accepted == 0
        assert report.slices[0].dominant_reason == str(RejectionReason.UNKNOWN_FEE)

    def test_research_mode_is_declared_in_the_report(self, session: Session) -> None:
        cheap_basket(session)
        rendered = run_falsification(session).render()
        assert "RESEARCH MODE" in rendered
        assert "None are" in rendered

    def test_the_report_states_its_own_limits(self, session: Session) -> None:
        cheap_basket(session)
        rendered = run_falsification(session).render()
        assert "upper bound" in rendered
        assert "unverified" in rendered


class TestShadowExecution:
    def test_qualifying_baskets_are_shadow_executed(self, session: Session) -> None:
        cheap_basket(session)
        report = run_falsification(session, quantity=D("10"))
        assert report.slices[0].shadow_attempted >= 1

    def test_an_incomplete_fill_books_an_unwind_loss(self, session: Session) -> None:
        """A partial basket is a directional position, and it costs something
        to get back out of."""
        from arbbot.shadow import ShadowConfig

        cheap_basket(session)
        report = run_falsification(
            session,
            quantity=D("10"),
            shadow=ShadowConfig(level_vanish_probability=D("1")),
        )
        slice_ = report.slices[0]
        assert slice_.shadow_completed == 0
        assert slice_.realized <= 0

    def test_execution_never_beats_the_paper_edge(self, session: Session) -> None:
        """Every assumption in the shadow model makes the outcome worse, so a
        realised figure above the paper edge would mean the model is wrong."""
        cheap_basket(session)
        report = run_falsification(session, quantity=D("10"))
        slice_ = report.slices[0]
        if slice_.shadow_attempted:
            assert slice_.realized <= slice_.gross_edge + D("0.01")
