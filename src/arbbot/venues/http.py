"""One HTTP client, shared by every venue integration.

Extracted when a second venue arrived. Rate limiting, the retry ladder and the
circuit breaker are not Kalshi facts -- they are facts about talking to a
rate-limited public API over a network that sometimes stops answering, and this
project has already paid for each of them once:

* the limiter, because per-process limits against a per-address budget cost
  this system its venue access for three days;
* the retry ladder capped per caller, because a six-attempt ladder spent fifty
  minutes on dead series during a sweep;
* the breaker with a half-open probe, because a breaker that could not close
  turned a transient blip into a sixty-hour outage.

A second venue that reimplemented any of those would reimplement the bugs. So
the venue-specific part is now only the endpoints and the payload shapes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

__all__ = [
    "DEFAULT_RATE_LIMIT",
    "FetchedPayload",
    "RateLimiter",
    "VenueHttpClient",
    "VenueUnreachable",
]

DEFAULT_RATE_LIMIT: Final = 8

#: A 429 means we already lost. Back off hard rather than politely.
_BACKOFF_BASE_SECONDS: Final = 1.0
_BACKOFF_MAX_SECONDS: Final = 60.0
_MAX_ATTEMPTS: Final = 6

#: Consecutive transport failures before a client stops trying entirely.
#:
#: Deliberately small. A venue that answers TCP and then resets the TLS
#: handshake is refusing this address, not struggling, and no amount of
#: patience converts a refusal into a response -- it only lengthens the
#: refusal. Three in a row is already unambiguous.
_FAILURE_THRESHOLD: Final = 3

#: How long an open circuit waits before letting a single request through.
#:
#: The first version of this breaker had no such wait and never closed at all.
#: It protected the address exactly as intended -- and then a transient blip
#: tripped it, the venue recovered minutes later, and the collector spent sixty
#: hours refusing to try. A breaker that cannot close is not a safety device,
#: it is a single point of failure with good intentions.
#:
#: So: wait, then allow exactly one probe. A successful probe closes the
#: circuit; a failed one doubles the wait. That keeps the property worth having
#: -- a refusing venue is never hammered -- without turning every hiccup into
#: an outage that needs a human to notice.
_BREAKER_COOLDOWN_SECONDS: Final = 60.0
_BREAKER_COOLDOWN_MAX_SECONDS: Final = 900.0


class VenueUnreachable(RuntimeError):
    """The venue is refusing this address, and the client has stopped asking.

    Distinct from an ordinary transport error so a caller can tell "one request
    failed" from "we appear to be blocked and must stop", which need opposite
    responses: the first is retried, the second is escalated to a human.
    """


class RateLimiter:
    """Simple asynchronous rate limiter.

    Spaces requests by a minimum interval rather than modelling the venue's
    token bucket exactly. Under-approximating a rate limit is safe; a precise
    model that drifts is not.
    """

    def __init__(self, requests_per_second: int = DEFAULT_RATE_LIMIT) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_allowed = now + self._interval


@dataclass(frozen=True, slots=True)
class FetchedPayload:
    """A payload plus the moment it arrived.

    Receive time comes from the client rather than the venue: staleness is
    measured against when *we* saw the data, since a venue clock we do not
    control is not evidence about our own latency.
    """

    payload: dict[str, Any]
    received_ts: dt.datetime
    endpoint: str


class VenueHttpClient:
    """Rate-limited, retrying, breaker-guarded HTTP for one venue."""

    def __init__(
        self,
        base_url: str,
        *,
        requests_per_second: int = DEFAULT_RATE_LIMIT,
        timeout_seconds: float = 15.0,
        max_attempts: int = _MAX_ATTEMPTS,
        failure_threshold: int = _FAILURE_THRESHOLD,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """
        :param max_attempts: how many times to try a request before giving up.
            The default suits a one-off fetch. A caller on a short polling
            cadence should lower it: the full backoff ladder spends about
            thirty seconds before surrendering, which is far longer than a
            five-second poll interval and would stall every other market in
            the cycle behind one broken one.
        :param failure_threshold: consecutive *transport* failures before the
            circuit opens and this client stops trying. Transport failures are
            singled out because they are how refusal looks: on 2026-08-14 the
            venue answered TCP and then reset every TLS handshake, and the
            collector retried against that for fifteen hours.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max_attempts
        self.failure_threshold = failure_threshold
        self._consecutive_transport_failures = 0
        self._tripped_at: float | None = None
        self._cooldown = _BREAKER_COOLDOWN_SECONDS
        self._probing = False
        self._clock = clock or time.monotonic
        self._limiter = RateLimiter(requests_per_second)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json", "User-Agent": "arbbot/0.1 (research)"},
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- transport ------------------------------------------------------
    async def fetch(
        self, path: str, params: dict[str, Any] | None = None, *, base_url: str | None = None
    ) -> FetchedPayload:
        """Any public GET, rate-limited and retried like the rest.

        Exists so that callers such as the universe resolver cannot quietly
        open their own unlimited connection to the venue. One resolver pass
        touches every temperature series; done outside the limiter it earns a
        429 immediately, and a refresh mid-run would then throttle the
        collector that depends on the same bucket.

        :param base_url: override for venues that split their API across hosts.
            Polymarket serves market definitions and order books from different
            ones, and they share a rate budget because they share an address --
            which is why this is an argument here rather than a second client.
        """
        body, received = await self._get(path, params or {}, base_url=base_url)
        if not isinstance(body, dict):
            raise ValueError(f"{path} returned {type(body).__name__}, expected an object")
        return FetchedPayload(payload=body, received_ts=received, endpoint=path)

    async def fetch_raw(
        self, path: str, params: dict[str, Any] | None = None, *, base_url: str | None = None
    ) -> Any:
        """The decoded body, whatever shape it is.

        Some endpoints answer with a bare JSON array. Those payloads cannot be
        archived as they stand -- every archived message in this system is an
        object with a known shape, and relaxing that for one venue would weaken
        the contract for all of them -- so this returns the body and leaves
        wrapping to the caller that knows what it means.
        """
        body, _ = await self._get(path, params or {}, base_url=base_url)
        return body

    async def _get(
        self, path: str, params: dict[str, Any], *, base_url: str | None = None
    ) -> tuple[Any, dt.datetime]:
        url = f"{base_url or self.base_url}{path}"
        delay = _BACKOFF_BASE_SECONDS

        if self._tripped_at is not None:
            waited = self._clock() - self._tripped_at
            if waited < self._cooldown:
                raise VenueUnreachable(
                    f"circuit open at {self.base_url} after "
                    f"{self._consecutive_transport_failures} consecutive transport failures. "
                    f"A venue that stops completing TLS handshakes is refusing this address, "
                    f"and continuing to knock lengthens the refusal. Next probe in "
                    f"{self._cooldown - waited:.0f}s."
                )
            # Half open: exactly one request goes through. Success closes the
            # circuit; failure doubles the wait before the next probe.
            self._probing = True

        for attempt in range(1, self.max_attempts + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.get(url, params=params)
            except httpx.TransportError:
                # A transport failure is not a slow venue. TCP connected and the
                # peer reset the handshake, or nothing answered at all -- and on
                # 2026-08-14 that was the venue blocking this address while the
                # collector patiently retried for fifteen hours.
                self._consecutive_transport_failures += 1
                if self._probing:
                    # The probe failed: still refused. Wait longer rather than
                    # settling into a fixed rhythm of knocking.
                    self._cooldown = min(self._cooldown * 2, _BREAKER_COOLDOWN_MAX_SECONDS)
                    self._tripped_at = self._clock()
                    self._probing = False
                elif self._consecutive_transport_failures >= self.failure_threshold:
                    self._tripped_at = self._clock()
                if attempt == self.max_attempts:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_MAX_SECONDS)
                continue
            else:
                # Any answer at all -- a 404 included -- means the venue is
                # talking to this address again, which is the only thing this
                # breaker was ever measuring.
                self._consecutive_transport_failures = 0
                self._tripped_at = None
                self._cooldown = _BREAKER_COOLDOWN_SECONDS
                self._probing = False

            # The venue publishes no Retry-After; the bucket refills
            # continuously, so exponential backoff is the documented remedy.
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS or (
                response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
            ):
                if attempt == self.max_attempts:
                    response.raise_for_status()
                await asyncio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_MAX_SECONDS)
                continue

            response.raise_for_status()
            return response.json(), dt.datetime.now(dt.UTC)

        raise RuntimeError("unreachable: retry loop exited without returning")
