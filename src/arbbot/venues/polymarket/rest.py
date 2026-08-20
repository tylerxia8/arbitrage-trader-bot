"""Polymarket public REST endpoints.

Two hosts, because the venue splits metadata from books: ``gamma`` serves
market definitions and settlement prose, ``clob`` serves order books keyed by
outcome token rather than by market. Both are public and unauthenticated for
reads, which is what this project needs and all it asks for.

Transport -- limiter, retry ladder, circuit breaker -- comes from
:mod:`arbbot.venues.http` unchanged. That is the point of having extracted it:
a second venue that reimplemented any of those would reimplement the three
outages that shaped them.
"""

from __future__ import annotations

from typing import Any, Final

from arbbot.venues.http import FetchedPayload, VenueHttpClient
from arbbot.venues.polymarket.parse import SCHEMA_VERSION, VENUE

__all__ = ["CLOB_BASE", "GAMMA_BASE", "PolymarketRestClient"]

#: Verified against the live API on 2026-08-19.
GAMMA_BASE: Final = "https://gamma-api.polymarket.com"
CLOB_BASE: Final = "https://clob.polymarket.com"

#: Well below anything published. The venue budget lease is per venue, so this
#: does not contend with Kalshi -- but the sum still has to be owned somewhere,
#: and that lesson cost three days of access.
DEFAULT_RATE_LIMIT: Final = 5


class PolymarketRestClient(VenueHttpClient):
    """Public market-data client. Never sends credentials."""

    def __init__(
        self, base_url: str = GAMMA_BASE, *, clob_url: str = CLOB_BASE, **kwargs: Any
    ) -> None:
        kwargs.setdefault("requests_per_second", DEFAULT_RATE_LIMIT)
        super().__init__(base_url, **kwargs)
        self.venue = VENUE
        self.schema_version = SCHEMA_VERSION
        self.clob_url = clob_url.rstrip("/")

    async def fetch_markets(
        self, *, limit: int = 100, closed: bool = False, offset: int = 0
    ) -> list[dict[str, Any]]:
        """One page of market definitions.

        Returns the list directly. This endpoint answers with a bare JSON
        array rather than an object, which the shared client refuses -- every
        archived payload in this system is an object with a known shape, and
        relaxing that for one venue would weaken the archive's contract for
        all of them. So the list is fetched here and wrapped by the caller if
        it is going to be archived.
        """
        payload = await self.fetch_raw(
            "/markets", {"limit": limit, "closed": str(closed).lower(), "offset": offset}
        )
        return payload if isinstance(payload, list) else []

    async def fetch_book(self, token_id: str) -> FetchedPayload:
        """Order book for one outcome token.

        Keyed by token rather than by market: a binary market has two of these
        and they are separate books, not two sides of one.
        """
        return await self.fetch("/book", {"token_id": token_id}, base_url=self.clob_url)
