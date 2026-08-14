"""One venue budget, shared across every process that spends it (NFR-01).

The venue limits requests per IP. This system limited them per component. On
2026-08-14 that difference cost the project its access: a collector at roughly
four requests a second, a one-second probe at six, a proposal sweep and a
venue-wide survey at five ran concurrently, each politely under the ceiling its
own limiter knew about, and together well over the one the venue enforces. The
production host started resetting TLS handshakes, fifteen and a half hours of
collection were lost, and the seven-day exit gate went with it.

The lesson is narrow and worth stating exactly: **a rate limit enforced per
process is not enforced.** Every consumer here was correct in isolation. The
sum was the thing nobody owned.

So the budget is claimed, not assumed. Before its first request a consumer
leases a share; if the live total would exceed the venue ceiling the lease is
refused and the consumer does not start. Refusing to run is the right outcome
-- a survey that does not run costs a snapshot, and a survey that runs anyway
costs the archive.

Leases heartbeat rather than being held to completion, because the failure mode
that matters is a consumer dying without releasing one. A stale lease expires
and its share returns to the pool. The alternative -- a lease released only on
clean shutdown -- means one crash locks the venue out until a human notices,
which is a worse outage than the one it prevents.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
from dataclasses import dataclass
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from arbbot.collection.health import utc_now
from arbbot.db.models import VenueLease

__all__ = [
    "DEFAULT_CEILING",
    "LEASE_TTL",
    "BudgetExceeded",
    "LeaseHandle",
    "acquire_lease",
    "active_leases",
    "release_lease",
]

#: Requests per second this system will collectively spend at the venue.
#:
#: The Basic read tier refills 200 tokens a second and most calls cost 10, so
#: the venue's own ceiling is about twenty. This sits at half of that. The
#: margin is not politeness -- the 2026-08-14 block happened while every
#: individual component believed it was well inside the limit, and the cost of
#: being wrong is measured in days of lost collection, not in latency.
DEFAULT_CEILING: Final = 10

#: How long a lease survives without a heartbeat before its share is reclaimed.
LEASE_TTL: Final = dt.timedelta(minutes=2)


class BudgetExceeded(RuntimeError):
    """Starting this consumer would put the venue over its request ceiling."""


@dataclass(frozen=True, slots=True)
class LeaseHandle:
    """A granted share of the venue budget."""

    lease_id: int
    venue: str
    consumer: str
    requests_per_second: int


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _expire_stale(session: Session, venue: str, *, now: dt.datetime) -> None:
    session.execute(
        delete(VenueLease).where(
            VenueLease.venue == venue, VenueLease.heartbeat_ts < now - LEASE_TTL
        )
    )


def active_leases(
    session: Session, venue: str, *, now: dt.datetime | None = None
) -> list[VenueLease]:
    """Every lease still heartbeating against ``venue``."""
    at = now or utc_now()
    return list(
        session.execute(
            select(VenueLease).where(
                VenueLease.venue == venue, VenueLease.heartbeat_ts >= at - LEASE_TTL
            )
        ).scalars()
    )


def acquire_lease(
    session: Session,
    *,
    venue: str,
    consumer: str,
    requests_per_second: int,
    ceiling: int = DEFAULT_CEILING,
    now: dt.datetime | None = None,
) -> LeaseHandle:
    """Claim a share of the venue budget, or refuse to start.

    :raises BudgetExceeded: when the live total plus this consumer's rate would
        exceed ``ceiling``. The caller must not fall back to a smaller rate on
        its own -- that is how the original failure happened, with every
        component choosing a number it considered reasonable.
    """
    if requests_per_second <= 0:
        raise ValueError("a consumer that claims no budget would spend it unmeasured")

    at = now or utc_now()
    _expire_stale(session, venue, now=at)

    committed = sum(lease.requests_per_second for lease in active_leases(session, venue, now=at))
    if committed + requests_per_second > ceiling:
        holders = ", ".join(
            f"{lease.consumer}@{lease.owner} ({lease.requests_per_second}/s)"
            for lease in active_leases(session, venue, now=at)
        )
        raise BudgetExceeded(
            f"{consumer} wants {requests_per_second}/s at {venue}, but {committed}/s of the "
            f"{ceiling}/s ceiling is already leased by: {holders or 'nothing'}. "
            f"Refusing to start: the last time these were added up after the fact, the "
            f"venue blocked this address and fifteen hours of collection were lost."
        )

    lease = VenueLease(
        venue=venue,
        consumer=consumer,
        requests_per_second=requests_per_second,
        owner=_owner(),
        started_ts=at,
        heartbeat_ts=at,
    )
    session.add(lease)
    session.flush()
    return LeaseHandle(
        lease_id=lease.id,
        venue=venue,
        consumer=consumer,
        requests_per_second=requests_per_second,
    )


def heartbeat(session: Session, handle: LeaseHandle, *, now: dt.datetime | None = None) -> bool:
    """Keep a lease alive. Returns whether it still existed to refresh."""
    lease = session.get(VenueLease, handle.lease_id)
    if lease is None:
        return False
    lease.heartbeat_ts = now or utc_now()
    session.flush()
    return True


def release_lease(session: Session, handle: LeaseHandle) -> None:
    """Return a share to the pool."""
    session.execute(delete(VenueLease).where(VenueLease.id == handle.lease_id))
    session.flush()
