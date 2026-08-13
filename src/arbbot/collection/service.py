"""The collection loop.

Runs a set of :class:`~arbbot.collection.collector.MarketCollector` instances
on a fixed cadence, committing each cycle and sampling feed health on its own
timer.

The exit gate for this milestone is seven days of continuous collection, so
the loop is written around the failure modes that only appear over days rather
than minutes:

*   **One market must not stop the run.** A single ticker that starts returning
    500s, or gets delisted mid-run, is counted and skipped. Losing six days of
    collection because one contract expired would be an absurd way to fail.
*   **Each cycle commits on its own.** A crash costs the cycle in flight, not
    the run.
*   **Health is sampled on a timer, not per message.** A collector that has
    stopped writes nothing at all, which is indistinguishable from a quiet
    market unless something independent is recording the silence.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from arbbot.collection.collector import MarketCollector, PollOutcome
from arbbot.collection.health import utc_now
from arbbot.venues.kalshi import KalshiAdapter
from arbbot.venues.kalshi.rest import KalshiRestClient

__all__ = ["CollectionService", "CycleReport"]


@dataclass(slots=True)
class CycleReport:
    """Outcome of one pass over every market."""

    started_ts: dt.datetime
    stored: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def polled(self) -> int:
        return self.stored + self.unchanged + self.failed

    @property
    def all_failed(self) -> bool:
        """Every market failed -- a venue outage or a broken deployment, not
        an unlucky ticker."""
        return self.polled > 0 and self.failed == self.polled


class CollectionService:
    """Polls a fixed set of markets until stopped."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        client: KalshiRestClient,
        tickers: Sequence[str],
        poll_interval_seconds: float = 5.0,
        health_interval_seconds: float = 30.0,
        adapter: KalshiAdapter | None = None,
    ) -> None:
        if not tickers:
            raise ValueError("a collector with no markets would report itself healthy forever")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self._session_factory = session_factory
        self._client = client
        self._poll_interval = poll_interval_seconds
        self._health_interval = health_interval_seconds
        adapter = adapter or KalshiAdapter()
        # One poll may not outlast the cycle it belongs to. Otherwise a single
        # broken market holds the whole cycle open through the client's backoff
        # ladder, and every other market's sampling cadence slips with it.
        self.collectors = [
            MarketCollector(
                ticker=ticker,
                client=client,
                adapter=adapter,
                poll_deadline_seconds=poll_interval_seconds,
            )
            for ticker in tickers
        ]
        self._last_health_sample: dt.datetime | None = None

    async def run_cycle(self, *, now: dt.datetime | None = None) -> CycleReport:
        """Poll every market once and persist the results."""
        at = now or utc_now()
        report = CycleReport(started_ts=at)

        with self._session_factory() as session:
            for collector in self.collectors:
                # Pass the cycle's clock down so an injected time governs the
                # whole cycle. Without it a test's fixed `now` and the real
                # receive stamps disagree, and the health sample below computes
                # a lag against a message that appears to arrive in the future.
                result = await collector.poll_once(session, now=now)
                match result.outcome:
                    case PollOutcome.STORED:
                        report.stored += 1
                    case PollOutcome.UNCHANGED:
                        report.unchanged += 1
                    case PollOutcome.FAILED:
                        report.failed += 1
                        if result.error:
                            report.errors.append(f"{result.ticker}: {result.error}")

            if self._should_sample_health(at):
                # Stamped after polling, not at cycle start: messages arrive
                # during the loop, and sampling against the older timestamp
                # reports a negative lag for every one of them.
                sampled_at = now or utc_now()
                for collector in self.collectors:
                    session.add(collector.health.sample(now=sampled_at))
                self._last_health_sample = sampled_at

            session.commit()

        return report

    def _should_sample_health(self, at: dt.datetime) -> bool:
        if self._last_health_sample is None:
            return True
        return (at - self._last_health_sample).total_seconds() >= self._health_interval

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """Collect until cancelled or ``stop`` is set.

        The interval is measured from the start of each cycle, not the end, so
        a slow cycle does not push every later one later. If a cycle overruns
        the interval the next starts immediately rather than accumulating drift
        -- over seven days, a few seconds of drift per cycle compounds into a
        sampling cadence nobody chose.
        """
        loop = asyncio.get_running_loop()
        while stop is None or not stop.is_set():
            started = loop.time()
            await self.run_cycle()
            elapsed = loop.time() - started
            delay = max(0.0, self._poll_interval - elapsed)

            if stop is None:
                await asyncio.sleep(delay)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    def resume_all(self) -> dict[str, int]:
        """Restore every stream's sequence from the archive before collecting."""
        resumed: dict[str, int] = {}
        with self._session_factory() as session:
            for collector in self.collectors:
                resumed[collector.ticker] = collector.resume(session)
        return resumed
