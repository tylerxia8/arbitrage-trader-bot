"""Pricing registered baskets as the books arrive.

The property worth defending here is the separation of two questions that a
single boolean would have blurred: whether the economics cleared, and whether
anyone is allowed to act on it. Every other test is about not guessing --
freshness that was not measured, legs that were not confirmed.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.db.models import Evaluation
from arbbot.detector.live import DetectionReport, LiveDetector
from arbbot.marketdata.book import OrderBook
from arbbot.marketdata.types import BookSide, PriceLevel
from arbbot.reasons import RejectionReason
from arbbot.registry import RelationshipRegistry
from arbbot.relationships import RelationshipType

D = Decimal
T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)
EVENT = "KXHIGHTEST-26AUG14"
LEGS = ("A", "B", "C")


def book_at(yes_ask: str, size: str = "1000") -> OrderBook:
    """A book whose implied YES ask is ``yes_ask`` (stored as the NO bid)."""
    book = OrderBook("test")
    book.apply_snapshot([(BookSide.NO, PriceLevel(D("1.00") - D(yes_ask), D(size)))], sequence=1)
    return book


def books(yes_ask: str, size: str = "1000") -> dict[str, OrderBook]:
    return {f"{EVENT}-{leg}": book_at(yes_ask, size) for leg in LEGS}


def register(session: Session, *, approve: bool) -> None:
    registry = RelationshipRegistry(session)
    record = registry.draft(
        slug=f"kalshi:{EVENT}",
        relationship_type=RelationshipType.INTERVAL_PARTITION,
        legs=[{"ticker": f"{EVENT}-{leg}", "side": "yes", "ratio": 1} for leg in LEGS],
        payout_proof={"claim": "exactly one leg settles YES"},
        dependency_hashes={f"{EVENT}-{leg}": "h" * 64 for leg in LEGS},
    )
    if approve:
        registry.approve(record, reviewer="tester", evidence="read the settlement rules")


def confirmed(at: dt.datetime = T0) -> dict[str, dt.datetime]:
    return dict.fromkeys((f"{EVENT}-{leg}" for leg in LEGS), at)


def run(
    session: Session,
    detector: LiveDetector,
    *,
    books_: dict[str, OrderBook] | None = None,
    confirmed_ts: dict[str, dt.datetime] | None = None,
    now: dt.datetime = T0,
) -> DetectionReport:
    return detector.evaluate_cycle(
        session,
        books=books("0.20") if books_ is None else books_,
        confirmed_ts=confirmed() if confirmed_ts is None else confirmed_ts,
        now=now,
    )


class TestPermissionAndEconomicsAreSeparate:
    """One boolean could not carry both questions honestly.

    Meaning tradeability, an archive gathered while relationships sit in review
    records nothing but ``relationship_not_approved`` and cannot say whether an
    edge went past. Meaning economics, the table appears to describe
    opportunities nobody has signed for.
    """

    def test_a_pending_relationship_is_priced_but_never_tradeable(self, session: Session) -> None:
        register(session, approve=False)
        report = run(session, LiveDetector(quantity=D("10")))

        assert report.priced == 1
        assert report.accepted == 1, "the economics cleared"
        assert report.tradeable == 0, "and nobody has signed for it"

        row = session.execute(select(Evaluation)).scalar_one()
        assert row.accepted is True
        assert row.tradeable is False
        assert row.relationship_status == "pending"

    def test_an_approved_relationship_that_clears_is_tradeable(self, session: Session) -> None:
        register(session, approve=True)
        report = run(session, LiveDetector(quantity=D("10")))

        assert report.tradeable == 1
        assert session.execute(select(Evaluation)).scalar_one().tradeable is True

    def test_an_approved_relationship_that_does_not_clear_is_not(self, session: Session) -> None:
        register(session, approve=True)
        report = run(session, LiveDetector(quantity=D("10")), books_=books("0.40"))

        assert report.accepted == 0
        assert report.tradeable == 0

    def test_the_status_is_stamped_at_decision_time(self, session: Session) -> None:
        """Approving later must not make a past rejection look as though it had
        been tradeable all along."""
        register(session, approve=False)
        run(session, LiveDetector(quantity=D("10")))

        registry = RelationshipRegistry(session)
        record = registry.latest(f"kalshi:{EVENT}")
        assert record is not None
        registry.approve(record, reviewer="tester", evidence="read it later")

        row = session.execute(select(Evaluation)).scalar_one()
        assert row.relationship_status == "pending"
        assert row.tradeable is False


class TestRejectionsArePersisted:
    def test_a_rejection_is_written_with_its_reason(self, session: Session) -> None:
        """A table of acceptances is a list of successes with no denominator."""
        register(session, approve=True)
        run(session, LiveDetector(quantity=D("10")), books_=books("0.40"))

        row = session.execute(select(Evaluation)).scalar_one()
        assert row.accepted is False
        assert row.reason == str(RejectionReason.NONPOSITIVE_NET_EDGE)

    def test_the_decision_records_what_it_was_decided_under(self, session: Session) -> None:
        """NFR-03: a decision that cannot be re-derived from its own record is
        not auditable."""
        register(session, approve=True)
        run(session, LiveDetector(quantity=D("10")))

        row = session.execute(select(Evaluation)).scalar_one()
        assert row.fee_rule
        assert row.parser_version
        assert row.max_book_age_ms == 2000
        assert len(row.legs) == 3
        assert row.legs[0]["levels_used"] >= 1
        assert row.legs[0]["cost"]


class TestFreshness:
    def test_a_leg_the_cycle_did_not_confirm_makes_the_basket_unpriceable(
        self, session: Session
    ) -> None:
        """Guessing an age would defeat the freshness gate entirely, so a
        basket missing a confirmation is skipped rather than priced stale."""
        register(session, approve=True)
        partial = confirmed()
        partial.pop(f"{EVENT}-C")

        report = run(session, LiveDetector(quantity=D("10")), confirmed_ts=partial)
        assert report.priced == 0
        assert report.skipped_incomplete == 1
        assert session.execute(select(Evaluation)).all() == []

    def test_age_is_measured_from_the_confirmation(self, session: Session) -> None:
        register(session, approve=True)
        stale = confirmed(T0 - dt.timedelta(seconds=30))

        report = run(session, LiveDetector(quantity=D("10")), confirmed_ts=stale)
        assert report.accepted == 0
        assert report.dominant_reason == str(RejectionReason.STALE_QUOTE)

    def test_a_missing_book_makes_the_basket_unpriceable(self, session: Session) -> None:
        register(session, approve=True)
        partial = books("0.20")
        partial.pop(f"{EVENT}-C")

        report = run(session, LiveDetector(quantity=D("10")), books_=partial)
        assert report.skipped_incomplete == 1


class TestScope:
    def test_an_unregistered_event_is_not_priced(self, session: Session) -> None:
        """Detection prices what a reviewer has at least been asked about. A
        basket nobody has proposed is not a candidate."""
        report = run(session, LiveDetector(quantity=D("10")))
        assert report.priced == 0

    def test_a_retired_relationship_is_not_priced(self, session: Session) -> None:
        register(session, approve=True)
        registry = RelationshipRegistry(session)
        record = registry.latest(f"kalshi:{EVENT}")
        assert record is not None
        registry.reject(record, reviewer="tester", evidence="withdrawn")

        assert run(session, LiveDetector(quantity=D("10"))).priced == 0

    def test_a_nonpositive_quantity_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            LiveDetector(quantity=D("0"))
