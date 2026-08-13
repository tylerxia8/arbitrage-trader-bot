"""Feed health.

NFR-01 forbids a silent outage longer than two minutes and NFR-02 requires
every evaluation to record the age of the quote it used. Both come down to the
same discipline: measure the feed rather than assume it.

The distinction that matters here is between *quiet* and *dead*. A market with
no activity for a minute is normal; a socket that stopped delivering a minute
ago is an incident. They are indistinguishable from message counts alone,
which is why health is sampled on a timer -- a collector that has stopped
leaves a visible hole in ``feed_health`` instead of simply writing nothing
anywhere and looking, in the aggregate, exactly like a quiet market.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from arbbot.db.models import FeedHealth
from arbbot.marketdata.sequence import SequenceTracker

__all__ = ["DEFAULT_MAX_SILENCE", "Clock", "StreamHealth", "utc_now"]

Clock = Callable[[], dt.datetime]

#: NFR-01: no silent outage longer than two minutes.
DEFAULT_MAX_SILENCE: Final = dt.timedelta(minutes=2)


def utc_now() -> dt.datetime:
    """Current time, always timezone-aware."""
    return dt.datetime.now(dt.UTC)


@dataclass(slots=True)
class StreamHealth:
    """Live health counters for one subscription stream."""

    venue: str
    subscription_key: str
    tracker: SequenceTracker = field(default_factory=SequenceTracker)

    messages: int = 0
    reconnects: int = 0
    parse_errors: int = 0
    last_message_ts: dt.datetime | None = None

    max_silence: dt.timedelta = DEFAULT_MAX_SILENCE

    def observe_message(self, received_ts: dt.datetime) -> None:
        """Record a delivered message."""
        if received_ts.tzinfo is None:
            raise ValueError("received_ts must be timezone-aware")
        self.messages += 1
        # max(), not assignment: messages can be processed slightly out of
        # order under concurrency, and health must never appear to go
        # backwards in time.
        if self.last_message_ts is None or received_ts > self.last_message_ts:
            self.last_message_ts = received_ts

    def observe_reconnect(self) -> None:
        """Record a reconnect. The stream's sequence history is no longer
        meaningful, so the tracker restarts -- but its anomaly counters
        survive, because they describe the session, not the socket."""
        self.reconnects += 1
        self.tracker.reset()

    def observe_parse_error(self) -> None:
        self.parse_errors += 1

    def lag(self, now: dt.datetime | None = None, clock: Clock = utc_now) -> dt.timedelta | None:
        """Time since the last message, or ``None`` if none has arrived.

        Never negative. A message cannot arrive after the moment it is
        measured against, so a negative result means the two timestamps came
        from clocks that disagree -- and reporting "-182 ms of staleness"
        helps nobody diagnose that. Clamping keeps the metric interpretable;
        the ordering itself is the caller's responsibility.
        """
        if self.last_message_ts is None:
            return None
        elapsed = (now or clock()) - self.last_message_ts
        return max(elapsed, dt.timedelta(0))

    def lag_ms(self, now: dt.datetime | None = None, clock: Clock = utc_now) -> int | None:
        """Lag in whole milliseconds, for metrics and the health endpoint."""
        lag = self.lag(now, clock)
        return None if lag is None else int(lag.total_seconds() * 1000)

    def is_healthy(self, now: dt.datetime | None = None, clock: Clock = utc_now) -> bool:
        """Whether the stream is delivering.

        A stream that has never delivered a message is **not** healthy. The
        alternative -- treating "no data yet" as fine -- would make a
        collector that failed to subscribe at all look identical to one
        watching a quiet market.
        """
        lag = self.lag(now, clock)
        return lag is not None and lag <= self.max_silence

    def sample(self, now: dt.datetime | None = None, clock: Clock = utc_now) -> FeedHealth:
        """Take a persistable health sample."""
        at = now or clock()
        return FeedHealth(
            observed_ts=at,
            venue=self.venue,
            subscription_key=self.subscription_key,
            messages=self.messages,
            gaps=self.tracker.gaps,
            missing_messages=self.tracker.missing_messages,
            duplicates=self.tracker.duplicates,
            rewinds=self.tracker.rewinds,
            reconnects=self.reconnects,
            parse_errors=self.parse_errors,
            last_message_ts=self.last_message_ts,
            lag_ms=self.lag_ms(at),
            is_healthy=self.is_healthy(at),
        )
