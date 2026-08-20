"""Kalshi REST client.

Driven entirely through ``httpx.MockTransport``: the suite must not depend on
the venue being up, and a test that silently starts hitting production is a
test that will one day hit it with something other than a GET.

The retry and pagination tests matter most. A 429 during a seven-day
collection run leaves a hole in the archive that cannot be filled afterwards --
the venue does not re-send what we failed to ask for in time.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from arbbot.venues.http import _BACKOFF_BASE_SECONDS
from arbbot.venues.kalshi.rest import (
    PRODUCTION_REST_BASE,
    KalshiRestClient,
    RateLimiter,
    VenueUnreachable,
)


def client_with(handler: Any, **kwargs: Any) -> KalshiRestClient:
    transport = httpx.MockTransport(handler)
    return KalshiRestClient(
        client=httpx.AsyncClient(transport=transport),
        requests_per_second=10_000,  # keep tests fast; pacing is tested separately
        **kwargs,
    )


class TestOrderbook:
    async def test_fetches_and_returns_the_payload(self) -> None:
        body = {"orderbook_fp": {"yes_dollars": [["0.5900", "152.00"]], "no_dollars": []}}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/markets/TEST/orderbook")
            return httpx.Response(200, json=body)

        async with client_with(handler) as client:
            result = await client.fetch_orderbook("TEST")

        assert result.payload == body
        assert result.received_ts.tzinfo is not None

    async def test_requests_full_depth_by_default(self) -> None:
        """Partial depth understates what a fill costs: a basket's executable
        price depends on levels beyond the top of book."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"orderbook_fp": {}})

        async with client_with(handler) as client:
            await client.fetch_orderbook("TEST")

        assert seen["depth"] == "0"

    async def test_sends_no_credentials(self) -> None:
        """Milestone 1 runs before any key is authorised, and the config layer
        refuses one in the research environment."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, json={"orderbook_fp": {}})

        async with client_with(handler) as client:
            await client.fetch_orderbook("TEST")

        for header in ("kalshi-access-key", "kalshi-access-signature", "authorization"):
            assert header not in captured


class TestRetries:
    async def test_retries_after_a_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def no_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("arbbot.venues.http.asyncio.sleep", no_sleep)
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429, json={"error": "too many requests"})
            return httpx.Response(200, json={"orderbook_fp": {}})

        async with client_with(handler) as client:
            result = await client.fetch_orderbook("TEST")

        assert attempts == 3
        assert result.payload == {"orderbook_fp": {}}

    async def test_backoff_grows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The venue publishes no Retry-After and the bucket refills
        continuously, so exponential backoff is the documented remedy."""
        slept: list[float] = []

        async def no_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("arbbot.venues.http.asyncio.sleep", no_sleep)
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 4:
                return httpx.Response(429)
            return httpx.Response(200, json={})

        async with client_with(handler) as client:
            await client.fetch_orderbook("TEST")

        # The rate limiter sleeps too, in sub-millisecond slices; only the
        # backoff waits are under test here.
        backoffs = [s for s in slept if s >= _BACKOFF_BASE_SECONDS]
        assert backoffs == sorted(backoffs)
        assert backoffs[-1] > backoffs[0]
        assert len(backoffs) == 3

    async def test_retries_a_server_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("arbbot.venues.http.asyncio.sleep", no_sleep)
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503) if attempts == 1 else httpx.Response(200, json={})

        async with client_with(handler) as client:
            await client.fetch_orderbook("TEST")

        assert attempts == 2

    async def test_gives_up_eventually(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retrying forever would look like a healthy collector doing nothing."""

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("arbbot.venues.http.asyncio.sleep", no_sleep)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        async with client_with(handler) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.fetch_orderbook("TEST")

    async def test_a_client_error_is_not_retried(self) -> None:
        """A 404 will not become a 200 by asking again."""
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(404)

        async with client_with(handler) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await client.fetch_orderbook("MISSING")

        assert attempts == 1


class TestCircuitBreaker:
    """When a venue refuses this address, stop asking.

    On 2026-08-14 the production host answered TCP and then reset every TLS
    handshake. The collector retried against that for fifteen and a half hours,
    which produced no data and gave the venue fifteen hours of unwanted
    traffic. Patience does not convert a refusal into a response; it only
    lengthens the refusal.
    """

    async def test_repeated_transport_failures_open_the_circuit(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection reset")

        client = client_with(handler, max_attempts=1, failure_threshold=3)
        for _ in range(3):
            with pytest.raises(httpx.TransportError):
                await client.fetch_orderbook("KXTEST-1")

        with pytest.raises(VenueUnreachable, match="circuit open"):
            await client.fetch_orderbook("KXTEST-1")

    async def test_the_open_circuit_makes_no_request(self) -> None:
        """The whole point: an open circuit must stop the traffic, not merely
        relabel the error."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("connection reset")

        client = client_with(handler, max_attempts=1, failure_threshold=2)
        for _ in range(2):
            with pytest.raises(httpx.TransportError):
                await client.fetch_orderbook("KXTEST-1")
        before = calls

        with pytest.raises(VenueUnreachable):
            await client.fetch_orderbook("KXTEST-1")
        assert calls == before

    async def test_a_success_resets_the_count(self) -> None:
        """Two failures an hour apart with a success between them are a flaky
        network, not a block, and treating them as one would take a working
        collector offline."""
        responses = [httpx.ConnectError("reset"), None, httpx.ConnectError("reset")]

        def handler(request: httpx.Request) -> httpx.Response:
            outcome = responses.pop(0)
            if outcome is not None:
                raise outcome
            return httpx.Response(200, json={"orderbook": {"yes": [], "no": []}})

        client = client_with(handler, max_attempts=1, failure_threshold=2)
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("KXTEST-1")
        await client.fetch_orderbook("KXTEST-1")
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("KXTEST-1")

        assert client._tripped_at is None

    async def test_an_http_error_does_not_open_the_circuit(self) -> None:
        """A 404 is the venue answering. Only a transport failure is a refusal
        to talk, and conflating them would trip the breaker on one delisted
        market."""
        client = client_with(lambda r: httpx.Response(404), max_attempts=1, failure_threshold=1)
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                await client.fetch_orderbook("KXTEST-1")

        assert client._tripped_at is None


class TestPagination:
    async def test_follows_the_cursor(self) -> None:
        pages = [
            {"markets": [{"ticker": "A"}], "cursor": "c1"},
            {"markets": [{"ticker": "B"}], "cursor": "c2"},
            {"markets": [{"ticker": "C"}], "cursor": ""},
        ]
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            page = pages[calls]
            calls += 1
            return httpx.Response(200, json=page)

        async with client_with(handler) as client:
            markets = await client.iter_markets()

        assert [m["ticker"] for m in markets] == ["A", "B", "C"]

    async def test_a_repeating_cursor_terminates(self) -> None:
        """A server echoing one cursor forever would otherwise spin this loop
        until the rate limiter throttled it into a very slow infinite loop --
        the least debuggable failure available."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"markets": [{"ticker": "A"}], "cursor": "same"})

        async with client_with(handler) as client:
            markets = await client.iter_markets()

        assert len(markets) == 2  # first page, then the repeat is detected

    async def test_an_empty_page_terminates(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"markets": [], "cursor": "next"})

        async with client_with(handler) as client:
            assert await client.iter_markets() == []


class TestResponseValidation:
    async def test_a_non_object_body_is_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2, 3])

        async with client_with(handler) as client:
            with pytest.raises(ValueError, match="expected an object"):
                await client.fetch_orderbook("TEST")


class TestRateLimiter:
    def test_rejects_a_nonpositive_rate(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            RateLimiter(0)

    async def test_spaces_successive_requests(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("arbbot.venues.http.asyncio.sleep", record)
        limiter = RateLimiter(requests_per_second=2)

        await limiter.acquire()
        await limiter.acquire()

        assert slept, "second acquire should have waited"


class TestDefaults:
    def test_production_base_url_is_the_verified_one(self) -> None:
        assert PRODUCTION_REST_BASE == "https://external-api.kalshi.com/trade-api/v2"

    def test_trailing_slash_is_normalised(self) -> None:
        client = KalshiRestClient(base_url="https://example.test/v2/")
        assert client.base_url == "https://example.test/v2"


class TestBreakerRecovery:
    """A breaker that cannot close is a single point of failure.

    The first version had no cooldown and no way back. It protected the address
    exactly as designed, and then a transient blip tripped it, the venue
    recovered minutes later, and a collection run spent sixty hours refusing to
    make a request. These pin the way out.
    """

    async def test_the_circuit_stays_open_during_the_cooldown(self) -> None:
        now = [0.0]
        client = client_with(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("reset")),
            max_attempts=1,
            failure_threshold=1,
            clock=lambda: now[0],
        )
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("A")

        now[0] = 30.0
        with pytest.raises(VenueUnreachable, match="Next probe in"):
            await client.fetch_orderbook("A")

    async def test_one_probe_is_allowed_after_the_cooldown(self) -> None:
        now = [0.0]
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("reset")
            return httpx.Response(200, json={"orderbook": {}})

        client = client_with(handler, max_attempts=1, failure_threshold=1, clock=lambda: now[0])
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("A")

        now[0] = 61.0
        await client.fetch_orderbook("A")
        assert calls == 2

    async def test_a_successful_probe_closes_the_circuit(self) -> None:
        """The property that was missing. Recovery must not need a human."""
        now = [0.0]
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("reset")
            return httpx.Response(200, json={"orderbook": {}})

        client = client_with(handler, max_attempts=1, failure_threshold=1, clock=lambda: now[0])
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("A")
        now[0] = 61.0
        await client.fetch_orderbook("A")

        # Immediately afterwards, with no further waiting.
        await client.fetch_orderbook("A")
        assert client._tripped_at is None

    async def test_a_failed_probe_doubles_the_wait(self) -> None:
        """Otherwise an open circuit settles into a fixed rhythm of knocking,
        which is the behaviour the breaker exists to prevent."""
        now = [0.0]
        client = client_with(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("reset")),
            max_attempts=1,
            failure_threshold=1,
            clock=lambda: now[0],
        )
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("A")

        now[0] = 61.0
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("A")

        now[0] = 100.0
        with pytest.raises(VenueUnreachable):
            await client.fetch_orderbook("A")
        assert client._cooldown == 120.0

    async def test_an_http_answer_also_closes_it(self) -> None:
        """A 404 is the venue talking to this address, which is the only thing
        the breaker was measuring."""
        now = [0.0]
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("reset")
            return httpx.Response(404)

        client = client_with(handler, max_attempts=1, failure_threshold=1, clock=lambda: now[0])
        with pytest.raises(httpx.TransportError):
            await client.fetch_orderbook("A")
        now[0] = 61.0
        with pytest.raises(httpx.HTTPStatusError):
            await client.fetch_orderbook("A")

        assert client._tripped_at is None
