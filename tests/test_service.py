"""The collection loop.

Scoped to the failure modes that only show up over days: one market breaking
without taking the run with it, health being sampled even when nothing is
happening, and the loop not drifting.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from arbbot.collection.service import CollectionService
from arbbot.db.base import Base
from arbbot.db.models import FeedHealth, RawMessage
from arbbot.venues.kalshi.rest import KalshiRestClient

T0 = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC)

GOOD = {"orderbook_fp": {"yes_dollars": [["0.5900", "10.00"]], "no_dollars": [["0.4000", "5.00"]]}}


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def service_over(
    factory: sessionmaker[Session],
    handler: Any,
    tickers: list[str],
    **kwargs: Any,
) -> CollectionService:
    client = KalshiRestClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        requests_per_second=10_000,
        # No retry ladder in tests: the real one sleeps for tens of seconds,
        # which is the behaviour under test in test_kalshi_rest, not here.
        max_attempts=1,
    )
    return CollectionService(session_factory=factory, client=client, tickers=tickers, **kwargs)


def count(factory: sessionmaker[Session], model: Any) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(model)).scalar_one()


class TestConfiguration:
    def test_refuses_an_empty_market_list(self, factory: sessionmaker[Session]) -> None:
        """A collector with nothing to collect would report itself healthy
        forever while producing no evidence at all."""
        with pytest.raises(ValueError, match="no markets"):
            service_over(factory, lambda r: httpx.Response(200, json=GOOD), [])

    def test_refuses_a_nonpositive_interval(self, factory: sessionmaker[Session]) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            service_over(
                factory,
                lambda r: httpx.Response(200, json=GOOD),
                ["A"],
                poll_interval_seconds=0,
            )


class TestCycle:
    async def test_polls_every_market(self, factory: sessionmaker[Session]) -> None:
        service = service_over(
            factory, lambda r: httpx.Response(200, json=GOOD), ["AAA", "BBB", "CCC"]
        )
        report = await service.run_cycle(now=T0)

        assert report.polled == 3
        assert report.stored == 3
        assert count(factory, RawMessage) == 3

    async def test_commits_each_cycle(self, factory: sessionmaker[Session]) -> None:
        """A crash costs the cycle in flight, not the run."""
        service = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"])
        await service.run_cycle(now=T0)
        assert count(factory, RawMessage) == 1

    async def test_a_repeat_cycle_stores_nothing_new(self, factory: sessionmaker[Session]) -> None:
        service = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"])
        await service.run_cycle(now=T0)
        report = await service.run_cycle(now=T0 + dt.timedelta(seconds=5))

        assert report.unchanged == 1
        assert count(factory, RawMessage) == 1


class TestIsolation:
    async def test_one_broken_market_does_not_stop_the_others(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Losing six days of collection because one contract expired would be
        an absurd way to fail the exit gate."""

        def handler(request: httpx.Request) -> httpx.Response:
            if "BROKEN" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, json=GOOD)

        service = service_over(factory, handler, ["AAA", "BROKEN", "CCC"])
        report = await service.run_cycle(now=T0)

        assert report.stored == 2
        assert report.failed == 1
        assert not report.all_failed
        assert count(factory, RawMessage) == 2

    async def test_a_total_outage_is_distinguishable(self, factory: sessionmaker[Session]) -> None:
        """Every market failing is a venue outage or a broken deployment, not
        an unlucky ticker -- and the report says so."""
        service = service_over(factory, lambda r: httpx.Response(503), ["AAA", "BBB"])
        report = await service.run_cycle(now=T0)

        assert report.all_failed
        assert len(report.errors) == 2

    async def test_errors_name_the_market(self, factory: sessionmaker[Session]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return (
                httpx.Response(404)
                if "BROKEN" in request.url.path
                else httpx.Response(200, json=GOOD)
            )

        service = service_over(factory, handler, ["AAA", "BROKEN"])
        report = await service.run_cycle(now=T0)
        assert any("BROKEN" in error for error in report.errors)


class TestHealthSampling:
    async def test_samples_on_the_first_cycle(self, factory: sessionmaker[Session]) -> None:
        service = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"])
        await service.run_cycle(now=T0)
        assert count(factory, FeedHealth) == 1

    async def test_does_not_sample_every_cycle(self, factory: sessionmaker[Session]) -> None:
        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            health_interval_seconds=30,
        )
        await service.run_cycle(now=T0)
        await service.run_cycle(now=T0 + dt.timedelta(seconds=5))
        assert count(factory, FeedHealth) == 1

    async def test_samples_again_after_the_interval(self, factory: sessionmaker[Session]) -> None:
        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            health_interval_seconds=30,
        )
        await service.run_cycle(now=T0)
        await service.run_cycle(now=T0 + dt.timedelta(seconds=31))
        assert count(factory, FeedHealth) == 2

    async def test_sampled_lag_is_never_negative(self, factory: sessionmaker[Session]) -> None:
        """Messages arrive *during* the cycle, so sampling against the cycle's
        start time reported them as arriving in the future."""
        service = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"])
        await service.run_cycle(now=T0)

        with factory() as session:
            sample = session.execute(select(FeedHealth)).scalar_one()
        assert sample.lag_ms is not None
        assert sample.lag_ms >= 0

    async def test_samples_even_when_every_poll_fails(self, factory: sessionmaker[Session]) -> None:
        """This is the whole point of sampling on a timer: a collector that is
        failing must leave a record saying so, not simply write nothing."""
        service = service_over(factory, lambda r: httpx.Response(503), ["AAA"])
        await service.run_cycle(now=T0)

        with factory() as session:
            sample = session.execute(select(FeedHealth)).scalar_one()
        assert sample.is_healthy is False
        assert sample.parse_errors == 1


class TestProbeIsolation:
    async def test_a_probe_archives_under_its_own_channel(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Two pollers at different cadences must not share a subscription key.

        They would collide on the archive's identity constraint and corrupt
        each other's resumed sequence -- so a one-second probe and the
        thirty-second collector are separate streams of the same market.
        """
        from arbbot.collection.collector import PROBE_CHANNEL

        broad = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"])
        probe = service_over(
            factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"], channel=PROBE_CHANNEL
        )

        assert broad.collectors[0].subscription_key != probe.collectors[0].subscription_key

    async def test_both_streams_archive_independently(self, factory: sessionmaker[Session]) -> None:
        from arbbot.collection.collector import PROBE_CHANNEL

        broad = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"])
        probe = service_over(
            factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"], channel=PROBE_CHANNEL
        )
        await broad.run_cycle(now=T0)
        await probe.run_cycle(now=T0)

        assert count(factory, RawMessage) == 2

    async def test_a_short_interval_does_not_starve_the_deadline(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A one-second probe with a one-second deadline would kill requests
        that were merely slow and record them as failures rather than data."""
        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            poll_interval_seconds=1.0,
        )
        deadline = service.collectors[0].poll_deadline_seconds
        assert deadline is not None
        assert deadline >= 3.0


class TestMarketRefresh:
    async def test_rotates_to_the_new_days_markets(self, factory: sessionmaker[Session]) -> None:
        """The rotation problem: daily markets settle overnight, and a
        collector on a literal ticker list spends the rest of the week polling
        dead contracts while reporting itself healthy."""
        live = ["DAY1-A", "DAY1-B"]

        async def source() -> list[str]:
            return live

        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["DAY0-A", "DAY0-B"],
            market_source=source,
            refresh_interval_seconds=0,
        )
        report = await service.run_cycle(now=T0)

        assert report.refresh is not None
        assert report.refresh.added == ("DAY1-A", "DAY1-B")
        assert report.refresh.removed == ("DAY0-A", "DAY0-B")
        assert {c.ticker for c in service.collectors} == {"DAY1-A", "DAY1-B"}

    async def test_surviving_markets_keep_their_state(self, factory: sessionmaker[Session]) -> None:
        """Rebuilding a surviving collector would restart its sequence
        mid-run and re-archive an unchanged book as though it were new."""

        async def source() -> list[str]:
            return ["AAA", "BBB"]

        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            market_source=source,
            refresh_interval_seconds=0,
        )
        await service.run_cycle(now=T0)
        original = next(c for c in service.collectors if c.ticker == "AAA")
        await service.run_cycle(now=T0 + dt.timedelta(seconds=5))

        assert next(c for c in service.collectors if c.ticker == "AAA") is original

    async def test_a_failed_refresh_keeps_the_current_markets(
        self, factory: sessionmaker[Session]
    ) -> None:
        """A venue hiccup during a refresh should cost freshness, never
        continuity -- losing the run because /events blipped would be absurd."""

        async def source() -> list[str]:
            raise httpx.ConnectError("venue unreachable")

        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            market_source=source,
            refresh_interval_seconds=0,
        )
        report = await service.run_cycle(now=T0)

        assert report.refresh is not None
        assert report.refresh.failed is not None
        assert [c.ticker for c in service.collectors] == ["AAA"]
        assert report.stored == 1

    async def test_an_empty_universe_is_refused(self, factory: sessionmaker[Session]) -> None:
        """Accepting it would leave the collector with nothing to poll while
        still reporting itself alive."""

        async def source() -> list[str]:
            return []

        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            market_source=source,
            refresh_interval_seconds=0,
        )
        report = await service.run_cycle(now=T0)

        assert report.refresh is not None
        assert "no live markets" in (report.refresh.failed or "")
        assert [c.ticker for c in service.collectors] == ["AAA"]

    async def test_refresh_respects_its_interval(self, factory: sessionmaker[Session]) -> None:
        calls = 0

        async def source() -> list[str]:
            nonlocal calls
            calls += 1
            return ["AAA"]

        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            market_source=source,
            refresh_interval_seconds=900,
        )
        await service.run_cycle(now=T0)
        await service.run_cycle(now=T0 + dt.timedelta(minutes=5))
        assert calls == 1

        await service.run_cycle(now=T0 + dt.timedelta(minutes=20))
        assert calls == 2

    async def test_no_source_means_no_refresh(self, factory: sessionmaker[Session]) -> None:
        service = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA"])
        report = await service.run_cycle(now=T0)
        assert report.refresh is None


class TestRunForever:
    async def test_stops_when_asked(self, factory: sessionmaker[Session]) -> None:
        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            poll_interval_seconds=0.01,
        )
        stop = asyncio.Event()

        async def stop_soon() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(service.run_forever(stop=stop), stop_soon())
        assert count(factory, RawMessage) >= 1

    async def test_survives_repeated_cycles(self, factory: sessionmaker[Session]) -> None:
        service = service_over(
            factory,
            lambda r: httpx.Response(200, json=GOOD),
            ["AAA"],
            poll_interval_seconds=0.01,
        )
        stop = asyncio.Event()

        async def stop_soon() -> None:
            await asyncio.sleep(0.08)
            stop.set()

        await asyncio.gather(service.run_forever(stop=stop), stop_soon())
        # One archived book; the rest are unchanged, which is the correct
        # behaviour and the reason the archive does not grow without bound.
        assert count(factory, RawMessage) == 1


class TestResume:
    async def test_resume_all_reports_each_stream(self, factory: sessionmaker[Session]) -> None:
        service = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA", "BBB"])
        await service.run_cycle(now=T0)

        restarted = service_over(factory, lambda r: httpx.Response(200, json=GOOD), ["AAA", "BBB"])
        assert restarted.resume_all() == {"AAA": 1, "BBB": 1}
