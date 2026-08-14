"""Stopping, and staying stopped (NFR-05).

The risk gate refuses candidates one at a time. That is necessary and it is not
a kill switch: a per-candidate check re-evaluates on every cycle, so a system
sitting at its daily loss limit spends the rest of the day asking again, and a
system that recovers a cent starts trading again without anyone deciding it
should. What is missing is something that **latches**.

So a halt, once tripped, stays tripped until a person clears it. That is the
entire design, and the rest follows from it:

**Clearing requires a name and a reason.** The same discipline the relationship
registry applies to approvals, for the same reason: a halt cleared by "the
system" is a halt that nobody examined. The record of who resumed trading and
what they had established is the only thing that distinguishes a considered
restart from a reflex.

**Automatic trips and the manual switch are the same mechanism.** An operator
pulling the switch and a daily loss limit tripping produce identical state, so
there is one thing to check before trading and one thing to clear afterwards.
Two mechanisms would mean two ways to be half-stopped.

**The halt is checked before anything is priced, not before anything is sent.**
Pricing a candidate that cannot be traded wastes nothing, but it produces
records of opportunities that were never real. The stop belongs at the top of
the loop.

**A halt with no reason is impossible to construct.** Tripping requires a
sentence describing what happened, because the person clearing it later has
only that sentence to work from.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from decimal import Decimal

from arbbot.collection.health import utc_now
from arbbot.money import ZERO

__all__ = ["HaltCause", "HaltState", "TradingHalt"]


class HaltCause(enum.StrEnum):
    """Why trading stopped."""

    MANUAL = "manual"
    """A person pulled the switch. Needs no further justification."""

    DAILY_LOSS = "daily_loss"
    RECONCILIATION = "reconciliation"
    """An intent could not be resolved. The system does not know what it holds."""

    FEED_OUTAGE = "feed_outage"
    """Market data stopped. Pricing on the last known book is pricing on a guess."""

    VENUE_UNREACHABLE = "venue_unreachable"
    """The venue is refusing or unresponsive. Retrying into that made things
    worse once already."""


@dataclass(frozen=True, slots=True)
class HaltState:
    """Whether trading is stopped, and what stopped it."""

    tripped: bool
    cause: HaltCause | None = None
    detail: str = ""
    since: dt.datetime | None = None

    @property
    def may_trade(self) -> bool:
        return not self.tripped

    def render(self) -> str:
        if not self.tripped:
            return "trading: permitted (no halt in force)"
        cause = self.cause.value if self.cause else "unknown"
        when = f"{self.since:%Y-%m-%d %H:%M:%S}Z" if self.since else "unknown time"
        return (
            f"trading: HALTED since {when}\n"
            f"  cause : {cause}\n"
            f"  detail: {self.detail}\n"
            f"  Clearing this requires a named person and what they established. "
            f"It will not clear on its own."
        )


@dataclass(slots=True)
class TradingHalt:
    """A latching stop. Trips automatically or by hand; clears only by hand."""

    _cause: HaltCause | None = field(default=None, init=False)
    _detail: str = field(default="", init=False)
    _since: dt.datetime | None = field(default=None, init=False)
    history: list[HaltState] = field(default_factory=list)

    @property
    def state(self) -> HaltState:
        return HaltState(
            tripped=self._cause is not None,
            cause=self._cause,
            detail=self._detail,
            since=self._since,
        )

    def trip(self, cause: HaltCause, detail: str, *, now: dt.datetime | None = None) -> HaltState:
        """Stop trading.

        :raises ValueError: without a detail. The person clearing this later has
            only that sentence to work from, and "halted" on its own tells them
            nothing about whether it is safe to resume.
        """
        if not detail.strip():
            raise ValueError(
                "a halt must say what happened; the person who clears it later has "
                "only this sentence to work from"
            )
        # The first cause is kept, not the latest. A daily-loss halt that is
        # then re-tripped by a feed outage is still, principally, a daily-loss
        # halt -- and overwriting it would erase the thing that actually needs
        # investigating.
        if self._cause is None:
            self._cause = cause
            self._detail = detail
            self._since = now or utc_now()
            self.history.append(self.state)
        return self.state

    def clear(self, *, operator: str, reason: str, now: dt.datetime | None = None) -> HaltState:
        """Resume trading, on a named person's authority.

        :raises ValueError: without both an operator and a reason. A halt
            cleared by "the system" is a halt nobody examined, and the record of
            who resumed and what they established is the only thing separating a
            considered restart from a reflex.
        """
        if not operator.strip():
            raise ValueError("clearing a halt requires a named person, not a service account")
        if not reason.strip():
            raise ValueError(
                "clearing a halt requires what was established; without it there is no "
                "way to tell a considered restart from a reflex"
            )
        del now
        self._cause = None
        self._detail = ""
        self._since = None
        return self.state

    # -- automatic trips --------------------------------------------------
    def check_daily_loss(
        self, realised_loss: Decimal, limit: Decimal, *, now: dt.datetime | None = None
    ) -> HaltState:
        """Latch at the daily loss limit.

        The risk gate already refuses individual candidates here. This is what
        makes the stop stick: without it, a system at its limit re-asks every
        cycle and resumes the moment a rounding movement puts it a cent back
        under, which is not a decision anyone made.
        """
        if limit > ZERO and realised_loss >= limit:
            return self.trip(
                HaltCause.DAILY_LOSS,
                f"realised loss ${realised_loss} reached the ${limit} daily limit",
                now=now,
            )
        return self.state

    def check_unresolved(self, unresolved: int, *, now: dt.datetime | None = None) -> HaltState:
        """Latch when an intent cannot be resolved.

        Reconciliation returning ``INCIDENT`` means the system does not know
        what it holds, and no amount of further trading improves that.
        """
        if unresolved > 0:
            return self.trip(
                HaltCause.RECONCILIATION,
                f"{unresolved} intent(s) could not be reconciled; the system does not "
                f"know what it holds",
                now=now,
            )
        return self.state

    def check_feed(
        self, silence: dt.timedelta, max_silence: dt.timedelta, *, now: dt.datetime | None = None
    ) -> HaltState:
        """Latch when market data has stopped.

        Pricing on the last book received is pricing on a guess about a market
        that has since moved, and the freshness gate would reject those
        candidates anyway -- but silently, one at a time, looking exactly like
        a quiet market rather than a broken feed.
        """
        if silence > max_silence:
            return self.trip(
                HaltCause.FEED_OUTAGE,
                f"no market data for {silence.total_seconds():.0f}s, over the "
                f"{max_silence.total_seconds():.0f}s limit",
                now=now,
            )
        return self.state
