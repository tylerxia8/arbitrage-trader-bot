"""Operator health view.

``GET /health`` answers one question: is this deployment collecting evidence
right now, and can I trust what it has collected?

It is built to fail loudly. The endpoint returns HTTP 503 when the system is
unhealthy rather than 200 with a sad payload, because a monitor that only
reads status codes is the most likely consumer, and a green tick on a dead
collector is worse than no monitoring at all.

Health is judged from the ``feed_health`` table rather than from in-process
counters, so the answer stays truthful when the API and the collector are
separate processes -- and so a collector that has died shows up as stale
samples instead of simply not contradicting anything.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot import __version__, buildflags
from arbbot.collection.health import DEFAULT_MAX_SILENCE, utc_now
from arbbot.config import Settings
from arbbot.db.models import FeedHealth

__all__ = ["build_health_payload", "router"]

router = APIRouter(tags=["health"])


def _latest_samples(session: Session, *, since: dt.datetime) -> list[FeedHealth]:
    """The most recent health sample per stream, for streams still reporting.

    ``since`` excludes retired streams. The collection universe is daily
    markets that settle overnight, so without this every market ever collected
    stays in the report for good, permanently stale, and the deployment reads
    unhealthy forever on the strength of contracts that expired last week.

    A stream that stopped because its market ended is not a fault. A collector
    that stopped entirely still shows up: every stream falls outside the
    window, the list comes back empty, and an empty list is unhealthy.

    De-duplicated in Python rather than with a window function -- this runs on
    a hundred-odd streams, and portability across the SQLite fixtures and
    PostgreSQL is worth more here than elegance.
    """
    rows = session.execute(
        select(FeedHealth)
        .where(FeedHealth.observed_ts >= since)
        .order_by(FeedHealth.observed_ts.desc())
    ).scalars()
    latest: dict[str, FeedHealth] = {}
    for row in rows:
        latest.setdefault(row.subscription_key, row)
    return list(latest.values())


def build_health_payload(
    session: Session,
    settings: Settings,
    *,
    now: dt.datetime | None = None,
    max_silence: dt.timedelta = DEFAULT_MAX_SILENCE,
) -> dict[str, Any]:
    """Assemble the health document and decide whether the system is healthy."""
    at = now or utc_now()
    # A generous multiple of the silence threshold: wide enough that a stream
    # briefly behind is still judged rather than quietly dropped, narrow enough
    # that yesterday's settled markets fall out of the picture.
    samples = _latest_samples(session, since=at - (max_silence * 4))

    streams: list[dict[str, Any]] = []
    for sample in samples:
        observed = sample.observed_ts
        if observed.tzinfo is None:  # SQLite hands back naive values
            observed = observed.replace(tzinfo=dt.UTC)
        sample_age = (at - observed).total_seconds()
        # A stale *sample* means the collector stopped writing, which is a
        # different failure from a stale feed -- and the more serious one,
        # because nothing is left to report the feed at all.
        stale_sample = sample_age > max_silence.total_seconds()
        streams.append(
            {
                "subscription_key": sample.subscription_key,
                "venue": sample.venue,
                "healthy": bool(sample.is_healthy) and not stale_sample,
                "sample_age_seconds": round(sample_age, 1),
                "sample_is_stale": stale_sample,
                "messages": sample.messages,
                "lag_ms": sample.lag_ms,
                "gaps": sample.gaps,
                "missing_messages": sample.missing_messages,
                "duplicates": sample.duplicates,
                "rewinds": sample.rewinds,
                "reconnects": sample.reconnects,
                "parse_errors": sample.parse_errors,
            }
        )

    # No streams at all is unhealthy. Treating "nothing configured" as fine
    # would make a collector that never started look identical to one watching
    # quiet markets.
    healthy = bool(streams) and all(s["healthy"] for s in streams)

    return {
        "healthy": healthy,
        "version": __version__,
        "environment": settings.environment.value,
        "checked_ts": at.isoformat(),
        "streams": streams,
        "execution_gates": {
            "live_execution_compiled_in": buildflags.LIVE_EXECUTION_COMPILED_IN,
            "demo_execution_compiled_in": buildflags.DEMO_EXECUTION_COMPILED_IN,
            "live_trading_enabled": settings.live_trading_enabled,
            "may_submit_live_orders": settings.may_submit_live_orders(),
            "note": "per-basket human approval is required in addition to these gates",
        },
    }


def get_session() -> Session:  # pragma: no cover -- replaced by app wiring
    raise NotImplementedError("session dependency must be overridden by the application")


def get_settings() -> Settings:  # pragma: no cover -- replaced by app wiring
    raise NotImplementedError("settings dependency must be overridden by the application")


@router.get("/health")
def health(
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    payload = build_health_payload(session, settings)
    if not payload["healthy"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return payload
