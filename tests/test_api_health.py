"""The health endpoint.

The status code is the contract. A monitor that reads only the code is the
most likely consumer, and a green tick on a dead collector is worse than no
monitoring at all -- so anything short of healthy returns 503.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.orm import Session

from arbbot.api import health as health_module
from arbbot.collection.health import utc_now
from arbbot.config import Settings
from arbbot.db.models import FeedHealth

T0 = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.UTC)


def settings() -> Settings:
    return Settings(_env_file=None)


def sample(
    *,
    key: str = "orderbook_poll:AAA",
    healthy: bool = True,
    observed: dt.datetime | None = None,
    **counters: int,
) -> FeedHealth:
    at = observed if observed is not None else utc_now()
    return FeedHealth(
        observed_ts=at,
        venue="kalshi",
        subscription_key=key,
        messages=counters.get("messages", 10),
        gaps=counters.get("gaps", 0),
        missing_messages=counters.get("missing_messages", 0),
        duplicates=counters.get("duplicates", 0),
        rewinds=counters.get("rewinds", 0),
        reconnects=counters.get("reconnects", 0),
        parse_errors=counters.get("parse_errors", 0),
        last_message_ts=at,
        lag_ms=counters.get("lag_ms", 100),
        is_healthy=healthy,
    )


@pytest.fixture
async def client(session: Session) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the real ASGI app.

    Starlette's TestClient now requires a separate ``httpx2`` package; going
    through ASGITransport avoids the extra dependency and exercises the same
    application object a server would. The client is closed on teardown -- a
    leaked one surfaces later as a warning attributed to an unrelated test,
    which is a genuinely miserable thing to debug.
    """
    app = FastAPI()
    app.dependency_overrides[health_module.get_session] = lambda: session
    app.dependency_overrides[health_module.get_settings] = settings
    app.include_router(health_module.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


class TestStatusCode:
    async def test_healthy_streams_return_200(
        self, session: Session, client: httpx.AsyncClient
    ) -> None:
        session.add(sample())
        session.flush()

        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["healthy"] is True

    async def test_an_unhealthy_stream_returns_503(
        self, session: Session, client: httpx.AsyncClient
    ) -> None:
        session.add(sample(healthy=False))
        session.flush()
        assert (await client.get("/health")).status_code == 503

    async def test_no_streams_at_all_returns_503(self, client: httpx.AsyncClient) -> None:
        """A collector that never started must not look like one watching
        quiet markets."""
        response = await client.get("/health")
        assert response.status_code == 503
        assert response.json()["healthy"] is False

    async def test_one_bad_stream_fails_the_whole_check(
        self, session: Session, client: httpx.AsyncClient
    ) -> None:
        session.add(sample(key="orderbook_poll:AAA", healthy=True))
        session.add(sample(key="orderbook_poll:BBB", healthy=False))
        session.flush()
        assert (await client.get("/health")).status_code == 503


class TestStaleSamples:
    def test_a_stale_sample_is_unhealthy_even_if_it_said_otherwise(self, session: Session) -> None:
        """A stale sample means the collector stopped writing, which is worse
        than a stale feed: nothing is left to report the feed at all."""
        session.add(sample(healthy=True, observed=T0))
        session.flush()

        # Five minutes: past the two-minute silence threshold, but still inside
        # the retirement window, so the stream is judged rather than dropped.
        payload = health_module.build_health_payload(
            session, settings(), now=T0 + dt.timedelta(minutes=5)
        )
        assert payload["healthy"] is False
        assert payload["streams"][0]["sample_is_stale"] is True

    def test_a_recent_sample_is_not_stale(self, session: Session) -> None:
        session.add(sample(healthy=True, observed=T0))
        session.flush()

        payload = health_module.build_health_payload(
            session, settings(), now=T0 + dt.timedelta(seconds=10)
        )
        assert payload["healthy"] is True
        assert payload["streams"][0]["sample_is_stale"] is False

    def test_only_the_newest_sample_per_stream_is_used(self, session: Session) -> None:
        session.add(sample(healthy=False, observed=T0))
        session.add(sample(healthy=True, observed=T0 + dt.timedelta(seconds=30)))
        session.flush()

        payload = health_module.build_health_payload(
            session, settings(), now=T0 + dt.timedelta(seconds=40)
        )
        assert len(payload["streams"]) == 1
        assert payload["healthy"] is True


class TestRetiredStreams:
    def test_a_settled_market_drops_out_of_the_report(self, session: Session) -> None:
        """Daily markets settle overnight. Without this every market ever
        collected stays in the report permanently stale, and the deployment
        reads unhealthy forever on the strength of expired contracts."""
        session.add(sample(key="orderbook_poll:YESTERDAY", healthy=True, observed=T0))
        session.add(
            sample(
                key="orderbook_poll:TODAY",
                healthy=True,
                observed=T0 + dt.timedelta(days=1),
            )
        )
        session.flush()

        payload = health_module.build_health_payload(
            session, settings(), now=T0 + dt.timedelta(days=1, seconds=30)
        )
        keys = {s["subscription_key"] for s in payload["streams"]}
        assert keys == {"orderbook_poll:TODAY"}
        assert payload["healthy"] is True

    def test_a_stopped_collector_is_still_caught(self, session: Session) -> None:
        """Retiring streams must not hide a dead collector: when every stream
        falls outside the window the list is empty, and empty is unhealthy."""
        session.add(sample(healthy=True, observed=T0))
        session.flush()

        payload = health_module.build_health_payload(
            session, settings(), now=T0 + dt.timedelta(hours=2)
        )
        assert payload["streams"] == []
        assert payload["healthy"] is False


class TestPayload:
    async def test_reports_the_execution_gates(
        self, session: Session, client: httpx.AsyncClient
    ) -> None:
        session.add(sample())
        session.flush()

        gates = (await client.get("/health")).json()["execution_gates"]
        assert gates["live_execution_compiled_in"] is False
        assert gates["may_submit_live_orders"] is False
        assert "human approval" in gates["note"]

    async def test_reports_feed_counters(self, session: Session, client: httpx.AsyncClient) -> None:
        session.add(sample(gaps=2, missing_messages=17, parse_errors=1))
        session.flush()

        stream = (await client.get("/health")).json()["streams"][0]
        assert stream["gaps"] == 2
        assert stream["missing_messages"] == 17
        assert stream["parse_errors"] == 1

    async def test_reports_environment_and_version(
        self, session: Session, client: httpx.AsyncClient
    ) -> None:
        session.add(sample())
        session.flush()

        body = (await client.get("/health")).json()
        assert body["environment"] == "local"
        assert body["version"]


@pytest.mark.parametrize("path", ["/orders", "/relationships/x/approve", "/kill"])
async def test_no_action_routes_exist_yet(client: httpx.AsyncClient, path: str) -> None:
    """Milestone 1 is read-only. An approval or kill endpoint that exists
    before there is anything to approve is just an unguarded door."""
    assert (await client.post(path)).status_code == 404
