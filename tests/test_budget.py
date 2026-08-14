"""One venue budget, shared across every process that spends it.

Every test here is the 2026-08-14 outage in miniature: components that were
each individually correct, summing to something that got the address blocked.
The property under test is that the sum is now owned by something.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from arbbot.venues.budget import (
    LEASE_TTL,
    BudgetExceeded,
    LeaseHandle,
    acquire_lease,
    active_leases,
    heartbeat,
    release_lease,
)

T0 = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)
VENUE = "kalshi"


def lease(
    session: Session, consumer: str, rate: int, *, at: dt.datetime = T0, ceiling: int = 10
) -> LeaseHandle:
    return acquire_lease(
        session,
        venue=VENUE,
        consumer=consumer,
        requests_per_second=rate,
        ceiling=ceiling,
        now=at,
    )


class TestSharing:
    def test_a_consumer_within_the_ceiling_starts(self, session: Session) -> None:
        handle = lease(session, "collector", 4)
        assert handle.requests_per_second == 4
        assert len(active_leases(session, VENUE, now=T0)) == 1

    def test_consumers_that_fit_together_all_start(self, session: Session) -> None:
        lease(session, "collector", 4)
        lease(session, "probe", 5)
        assert len(active_leases(session, VENUE, now=T0)) == 2

    def test_the_one_that_would_break_the_ceiling_is_refused(self, session: Session) -> None:
        """The outage, exactly. The collector, the probe and the survey were
        each reasonable; the fourth request per second was the one nobody
        owned."""
        lease(session, "collector", 4)
        lease(session, "probe", 5)

        with pytest.raises(BudgetExceeded, match="survey wants 5/s"):
            lease(session, "survey", 5)

    def test_the_refusal_names_who_holds_the_budget(self, session: Session) -> None:
        """An operator who cannot see what is holding the budget will just
        raise the ceiling, which is the same mistake with more steps."""
        lease(session, "collector", 8)

        with pytest.raises(BudgetExceeded, match="collector@"):
            lease(session, "probe", 6)

    def test_releasing_returns_the_share(self, session: Session) -> None:
        handle = lease(session, "collector", 8)
        with pytest.raises(BudgetExceeded):
            lease(session, "survey", 5)

        release_lease(session, handle)
        assert lease(session, "survey", 5).requests_per_second == 5


class TestStaleLeases:
    def test_a_lease_that_stopped_heartbeating_expires(self, session: Session) -> None:
        """A consumer that dies without releasing must not lock the venue out
        until a human notices -- that is a worse outage than the one leases
        prevent."""
        lease(session, "crashed", 8)
        later = T0 + LEASE_TTL + dt.timedelta(seconds=1)

        assert active_leases(session, VENUE, now=later) == []
        assert lease(session, "collector", 8, at=later).requests_per_second == 8

    def test_a_heartbeating_lease_does_not_expire(self, session: Session) -> None:
        handle = lease(session, "collector", 8)
        later = T0 + LEASE_TTL + dt.timedelta(seconds=1)
        heartbeat(session, handle, now=later)

        assert len(active_leases(session, VENUE, now=later)) == 1
        with pytest.raises(BudgetExceeded):
            lease(session, "survey", 5, at=later)

    def test_a_heartbeat_on_a_reclaimed_lease_reports_it(self, session: Session) -> None:
        """A consumer whose lease was reclaimed is spending budget nobody
        granted it, and it needs to find that out rather than carry on."""
        handle = lease(session, "collector", 4)
        release_lease(session, handle)
        assert heartbeat(session, handle) is False


class TestRefusals:
    def test_a_zero_rate_is_refused(self, session: Session) -> None:
        """A consumer that claims nothing would spend unmeasured, which is the
        original defect wearing a different hat."""
        with pytest.raises(ValueError, match="unmeasured"):
            lease(session, "sneaky", 0)

    def test_leases_are_per_venue(self, session: Session) -> None:
        acquire_lease(session, venue="other", consumer="collector", requests_per_second=10, now=T0)
        assert lease(session, "collector", 8).requests_per_second == 8
