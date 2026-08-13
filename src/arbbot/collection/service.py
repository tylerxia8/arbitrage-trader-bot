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
*   **The market set is refreshed, not fixed.** The recommended universe is
    daily temperature partitions, and they rotate: yesterday's Atlanta event
    already has zero active markets. A collector started on Monday with a
    literal ticker list spends Tuesday through Sunday polling settled
    contracts and archiving nothing, while reporting itself perfectly healthy.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from arbbot.collection.collector import MarketCollector, PollOutcome
from arbbot.collection.health import utc_now
from arbbot.venues.kalshi import KalshiAdapter
from arbbot.venues.kalshi.rest import KalshiRestClient

__all__ = ["CollectionService", "CycleReport", "MarketSource", "RefreshReport"]

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Tally:
    """Running totals between progress reports."""

    cycles: int = 0
    stored: int = 0
    unchanged: int = 0
    failed: int = 0


def _humanise(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


#: Resolves the currently-live market tickers. Async because it hits the venue.
MarketSource = Callable[[], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """Outcome of re-resolving the live market set."""

    at: dt.datetime
    added: tuple[str, ...]
    removed: tuple[str, ...]
    failed: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


@dataclass(slots=True)
class CycleReport:
    """Outcome of one pass over every market."""

    started_ts: dt.datetime
    stored: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    refresh: RefreshReport | None = None

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
        market_source: MarketSource | None = None,
        refresh_interval_seconds: float = 900.0,
        progress_interval_seconds: float = 600.0,
    ) -> None:
        """
        :param market_source: called periodically to re-resolve the live
            market set. Without one the ticker list is fixed, which is correct
            for a short run and wrong for a seven-day one against daily
            markets -- they settle overnight and the collector keeps polling
            contracts that no longer trade.
        """
        if not tickers and market_source is None:
            # An empty list is only acceptable when something will fill it. A
            # collector with nothing to poll and no way to find anything would
            # report itself healthy forever while producing no evidence.
            raise ValueError("a collector with no markets would report itself healthy forever")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self._session_factory = session_factory
        self._client = client
        self._poll_interval = poll_interval_seconds
        self._health_interval = health_interval_seconds
        self._market_source = market_source
        self._refresh_interval = refresh_interval_seconds
        self._last_refresh: dt.datetime | None = None
        adapter = adapter or KalshiAdapter()
        self._adapter = adapter
        # One poll may not outlast the cycle it belongs to. Otherwise a single
        # broken market holds the whole cycle open through the client's backoff
        # ladder, and every other market's sampling cadence slips with it.
        self.collectors = [self._make_collector(ticker) for ticker in tickers]
        self._last_health_sample: dt.datetime | None = None
        self._progress_interval = progress_interval_seconds
        self._last_progress = utc_now()
        self._since_progress = _Tally()

    def _make_collector(self, ticker: str) -> MarketCollector:
        return MarketCollector(
            ticker=ticker,
            client=self._client,
            adapter=self._adapter,
            poll_deadline_seconds=self._poll_interval,
        )

    async def refresh_markets(self, *, now: dt.datetime | None = None) -> RefreshReport:
        """Re-resolve the live market set and reconcile the collector list.

        Collectors for markets that survive the refresh are **kept**, not
        rebuilt. Their sequence counter, last-payload hash, and health history
        live in the object; discarding it would restart sequences mid-run and
        re-archive an unchanged book as though it were new.
        """
        at = now or utc_now()
        if self._market_source is None:
            return RefreshReport(at, added=(), removed=(), failed="no market source configured")

        try:
            live = list(await self._market_source())
        except Exception as exc:
            # Keep collecting the markets we already have. A venue hiccup
            # during a refresh should cost freshness, never continuity.
            return RefreshReport(at, added=(), removed=(), failed=f"{type(exc).__name__}: {exc}")

        if not live:
            return RefreshReport(at, added=(), removed=(), failed="venue returned no live markets")

        current = {c.ticker: c for c in self.collectors}
        wanted = set(live)

        added = tuple(sorted(wanted - current.keys()))
        removed = tuple(sorted(current.keys() - wanted))

        self.collectors = [current[t] for t in sorted(wanted & current.keys())] + [
            self._make_collector(t) for t in added
        ]
        self._last_refresh = at
        return RefreshReport(at, added=added, removed=removed)

    def _should_refresh(self, at: dt.datetime) -> bool:
        if self._market_source is None:
            return False
        if self._last_refresh is None:
            return True
        return (at - self._last_refresh).total_seconds() >= self._refresh_interval

    async def run_cycle(self, *, now: dt.datetime | None = None) -> CycleReport:
        """Poll every market once and persist the results."""
        at = now or utc_now()
        report = CycleReport(started_ts=at)

        if self._should_refresh(at):
            report.refresh = await self.refresh_markets(now=at)

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

    def _log_progress(self, report: CycleReport, at: dt.datetime) -> None:
        """Emit a periodic summary.

        A seven-day run is 20,000 cycles. Logging each one buries anything
        worth seeing; logging none of them leaves an operator with three
        startup lines and no way to tell a working collector from a wedged
        one. So: a heartbeat on a timer, plus anything unusual immediately.
        """
        if report.refresh is not None and (report.refresh.changed or report.refresh.failed):
            if report.refresh.failed:
                _log.warning("market refresh failed: %s", report.refresh.failed)
            else:
                _log.info(
                    "markets rotated: +%d -%d (now %d)",
                    len(report.refresh.added),
                    len(report.refresh.removed),
                    len(self.collectors),
                )

        if report.all_failed:
            _log.error("every market failed this cycle: %s", "; ".join(report.errors[:3]))
        elif report.failed:
            _log.warning(
                "%d of %d markets failed: %s", report.failed, report.polled, report.errors[0]
            )

        self._since_progress.stored += report.stored
        self._since_progress.unchanged += report.unchanged
        self._since_progress.failed += report.failed
        self._since_progress.cycles += 1

        if (at - self._last_progress).total_seconds() < self._progress_interval:
            return

        tally = self._since_progress
        _log.info(
            "%d cycles over %s: %d books archived, %d unchanged, %d failed, %d markets",
            tally.cycles,
            _humanise(at - self._last_progress),
            tally.stored,
            tally.unchanged,
            tally.failed,
            len(self.collectors),
        )
        self._since_progress = _Tally()
        self._last_progress = at

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
            report = await self.run_cycle()
            self._log_progress(report, utc_now())
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
