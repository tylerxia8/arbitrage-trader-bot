"""The second venue, and what it revealed about the first.

Most of these pin a difference. Kalshi and Polymarket are both binary
prediction markets and they model a book differently enough that code written
against one silently misprices the other.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from arbbot.marketdata.book import OrderBook
from arbbot.marketdata.types import BookSide
from arbbot.venues.polymarket import PolymarketRestClient, parse_book, parse_market

D = Decimal

BOOK = {
    "bids": [{"price": "0.041", "size": "1200"}, {"price": "0.040", "size": "800"}],
    "asks": [{"price": "0.043", "size": "500"}, {"price": "0.044", "size": "900"}],
}

MARKET = {
    "id": "559651",
    "conditionId": "0xabc",
    "question": "Xi Jinping out before 2027?",
    "description": "This market will resolve to Yes if ...",
    "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["323382", "256593"]',
    "endDate": "2026-12-31T00:00:00Z",
    "bestBid": 0.041,
    "bestAsk": 0.043,
    "orderPriceMinTickSize": 0.001,
    "liquidityNum": 245100.06,
    "active": True,
    "closed": False,
}


class TestBookShape:
    def test_asks_are_taken_as_published_not_derived(self) -> None:
        """The difference that matters most. Kalshi quotes resting bids on both
        sides and the YES ask is derived as a dollar minus the best NO bid.
        Polymarket quotes asks directly, and deriving them here would misprice
        every market by its spread."""
        levels = parse_book(BOOK)
        asks = [lvl.price_dollars for side, lvl in levels if side is BookSide.NO]

        assert D("0.043") in asks
        assert D("0.959") not in asks, "not 1.00 - best bid"

    def test_both_sides_are_decoded(self) -> None:
        levels = parse_book(BOOK)
        assert sum(1 for s, _ in levels if s is BookSide.YES) == 2
        assert sum(1 for s, _ in levels if s is BookSide.NO) == 2

    def test_prices_stay_exact(self) -> None:
        """A tick of 0.001 does not survive a float round trip intact, and this
        venue quotes at that grid."""
        levels = parse_book(BOOK)
        assert all(isinstance(lvl.price_dollars, Decimal) for _, lvl in levels)
        assert any(lvl.price_dollars == D("0.041") for _, lvl in levels)

    def test_zero_sized_levels_are_dropped(self) -> None:
        """A level with no size is a level that is not there."""
        levels = parse_book({"bids": [{"price": "0.5", "size": "0"}], "asks": []})
        assert levels == []

    def test_an_empty_book_decodes_to_nothing(self) -> None:
        assert parse_book({}) == []

    def test_the_book_reconstructs_through_the_shared_layer(self) -> None:
        """The point of the adapter: once decoded, a Polymarket book is just a
        book, and everything above the venue boundary is unchanged."""
        book = OrderBook("token")
        book.apply_snapshot(parse_book(BOOK), sequence=1)

        assert book.is_complete
        assert max(book.levels_by_side()[BookSide.YES]) == D("0.041")
        assert min(book.levels_by_side()[BookSide.NO]) == D("0.043")


class TestMarketShape:
    def test_json_encoded_arrays_are_decoded(self) -> None:
        """The venue returns ``outcomes`` and ``clobTokenIds`` as strings
        containing JSON, which reads as a list of characters if taken at face
        value."""
        mk = parse_market(MARKET)
        assert mk["outcomes"] == ["Yes", "No"]
        assert mk["token_ids"] == ["323382", "256593"]

    def test_a_binary_market_has_two_books(self) -> None:
        """Not one market with two sides. Each outcome token has its own."""
        assert len(parse_market(MARKET)["token_ids"]) == 2

    def test_the_settlement_prose_is_kept_whole(self) -> None:
        """It is the only account of settlement the venue gives, and a
        cross-venue pair lives or dies on whether it means the same as the
        other venue's prose."""
        assert parse_market(MARKET)["rules"].startswith("This market will resolve")

    def test_money_fields_are_decimal(self) -> None:
        mk = parse_market(MARKET)
        for key in ("best_bid", "best_ask", "tick_size", "liquidity"):
            assert isinstance(mk[key], Decimal), key

    def test_missing_prices_are_none_not_zero(self) -> None:
        """A market with no quote is not a market quoted at zero."""
        mk = parse_market({"id": "1", "question": "q"})
        assert mk["best_bid"] is None
        assert mk["best_ask"] is None


class TestClient:
    async def test_markets_come_back_as_a_list(self) -> None:
        """This endpoint answers with a bare JSON array, which the shared
        client refuses for anything archived."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[MARKET])

        client = PolymarketRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            assert len(await client.fetch_markets(limit=1)) == 1

    async def test_the_book_is_fetched_from_the_other_host(self) -> None:
        """Metadata and books live on different hosts that share an address,
        and therefore share a rate budget."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=BOOK)

        client = PolymarketRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            await client.fetch_book("323382")

        assert seen
        assert "clob.polymarket.com" in seen[0]
        assert "token_id=323382" in seen[0]

    async def test_an_object_endpoint_still_refuses_a_list(self) -> None:
        """The archive's contract is unchanged: everything stored is an object
        with a known shape."""
        client = PolymarketRestClient(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[1, 2]))
            ),
            requests_per_second=10_000,
            max_attempts=1,
        )
        async with client:
            with pytest.raises(ValueError, match="expected an object"):
                await client.fetch_book("x")
