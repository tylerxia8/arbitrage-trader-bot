"""The polling collector.

The tests that matter are about surviving days, not minutes: resuming
sequences across a restart, not re-archiving an unchanged book, and treating
a failed poll as a failure rather than as an empty market.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arbbot.collection.collector import MarketCollector, PollOutcome
from arbbot.db.models import BookSnapshot, RawMessage
from arbbot.marketdata.types import BookSide
from arbbot.venues.kalshi.rest import KalshiRestClient

TICKER = "KXTEST-MARKET"


def book(yes: list[tuple[str, str]], no: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [list(level) for level in yes],
            "no_dollars": [list(level) for level in no],
        }
    }


BOOK_A = book([("0.5900", "152.00")], [("0.4000", "11392.59")])
BOOK_B = book([("0.5800", "100.00")], [("0.4100", "50.25")])


def collector_serving(*payloads: dict[str, Any]) -> MarketCollector:
    """A collector whose client returns each payload in turn, then repeats
    the last one forever."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        payload = payloads[min(calls, len(payloads) - 1)]
        calls += 1
        return httpx.Response(200, json=payload)

    client = KalshiRestClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        requests_per_second=10_000,
        max_attempts=1,
    )
    return MarketCollector(ticker=TICKER, client=client)


def count(session: Session, model: Any) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


class TestFirstPoll:
    async def test_archives_and_snapshots(self, session: Session) -> None:
        result = await collector_serving(BOOK_A).poll_once(session)
        session.flush()

        assert result.outcome is PollOutcome.STORED
        assert result.sequence == 1
        assert count(session, RawMessage) == 1
        assert count(session, BookSnapshot) == 1

    async def test_snapshot_links_to_the_archived_payload(self, session: Session) -> None:
        """A snapshot that cannot name the payload it came from is not
        traceable evidence (FR-003)."""
        await collector_serving(BOOK_A).poll_once(session)
        session.flush()

        snapshot = session.execute(select(BookSnapshot)).scalar_one()
        raw = session.execute(select(RawMessage)).scalar_one()
        assert snapshot.raw_message_id == raw.id

    async def test_reconstructs_a_usable_book(self, session: Session) -> None:
        collector = collector_serving(BOOK_A)
        await collector.poll_once(session)

        assert collector.reconstructor.is_usable
        ask = collector.reconstructor.book.best_ask(BookSide.YES)
        assert ask is not None
        assert ask.price_dollars == Decimal("0.6000")

    async def test_levels_are_stored_as_exact_strings(self, session: Session) -> None:
        """A float round trip through JSON would undo the exactness the whole
        money path protects."""
        await collector_serving(BOOK_A).poll_once(session)
        session.flush()

        snapshot = session.execute(select(BookSnapshot)).scalar_one()
        assert snapshot.yes_levels == {"0.5900": "152.00"}
        assert snapshot.no_levels == {"0.4000": "11392.59"}
        for value in list(snapshot.yes_levels.values()) + list(snapshot.no_levels.values()):
            assert isinstance(value, str)


class TestUnchangedBooks:
    async def test_an_identical_book_is_not_re_archived(self, session: Session) -> None:
        collector = collector_serving(BOOK_A)
        first = await collector.poll_once(session)
        second = await collector.poll_once(session)
        session.flush()

        assert first.outcome is PollOutcome.STORED
        assert second.outcome is PollOutcome.UNCHANGED
        assert count(session, RawMessage) == 1

    async def test_an_unchanged_poll_still_counts_as_liveness(self, session: Session) -> None:
        """ "We polled and nothing changed" and "we stopped polling" must stay
        distinguishable, or a dead collector looks like a quiet market."""
        collector = collector_serving(BOOK_A)
        await collector.poll_once(session)
        await collector.poll_once(session)

        assert collector.health.messages == 2

    async def test_a_changed_book_is_archived_again(self, session: Session) -> None:
        collector = collector_serving(BOOK_A, BOOK_B)
        await collector.poll_once(session)
        result = await collector.poll_once(session)
        session.flush()

        assert result.outcome is PollOutcome.STORED
        assert result.sequence == 2
        assert count(session, RawMessage) == 2


class TestResume:
    async def test_resumes_the_sequence_after_a_restart(self, session: Session) -> None:
        """Restarting at zero would collide with the archive's identity
        constraint, and the collector would silently stop storing."""
        first = collector_serving(BOOK_A, BOOK_B)
        await first.poll_once(session)
        await first.poll_once(session)
        session.commit()

        restarted = collector_serving(BOOK_B)
        assert restarted.resume(session) == 2

        result = await restarted.poll_once(session)
        session.flush()
        assert result.sequence == 3
        assert count(session, RawMessage) == 3

    async def test_resume_on_an_empty_archive_starts_at_zero(self, session: Session) -> None:
        assert collector_serving(BOOK_A).resume(session) == 0

    async def test_resume_is_scoped_to_this_market(self, session: Session) -> None:
        """Another market's sequence numbers must not advance this one's."""
        other = MarketCollector(ticker="KXOTHER", client=collector_serving(BOOK_A).client)
        await other.poll_once(session)
        await other.poll_once(session)
        session.commit()

        assert collector_serving(BOOK_A).resume(session) == 0


class TestFailures:
    async def test_a_failed_request_is_not_an_empty_book(self, session: Session) -> None:
        """An empty book is a tradeable claim; a failed poll is not."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        collector = MarketCollector(ticker=TICKER, client=client)
        result = await collector.poll_once(session)
        session.flush()

        assert result.outcome is PollOutcome.FAILED
        assert count(session, RawMessage) == 0
        assert count(session, BookSnapshot) == 0
        assert not collector.reconstructor.is_usable

    async def test_a_failed_poll_is_counted(self, session: Session) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            requests_per_second=10_000,
            max_attempts=1,
        )
        collector = MarketCollector(ticker=TICKER, client=client)
        await collector.poll_once(session)

        assert collector.health.parse_errors == 1
        assert collector.health.messages == 0

    async def test_an_undecodable_payload_invalidates_the_book(self, session: Session) -> None:
        collector = collector_serving(BOOK_A, {"not_an_orderbook": True})
        await collector.poll_once(session)
        assert collector.reconstructor.is_usable

        result = await collector.poll_once(session)
        assert result.outcome is PollOutcome.FAILED
        assert not collector.reconstructor.is_usable

    async def test_a_malformed_price_does_not_end_collection(self, session: Session) -> None:
        bad = book([("not-a-price", "1.00")], [])
        collector = collector_serving(BOOK_A, bad, BOOK_B)

        await collector.poll_once(session)
        failed = await collector.poll_once(session)
        recovered = await collector.poll_once(session)

        assert failed.outcome is PollOutcome.FAILED
        assert recovered.outcome is PollOutcome.STORED
        assert collector.reconstructor.is_usable


class TestPollDeadline:
    async def test_a_hanging_market_is_bounded_in_time(self, session: Session) -> None:
        """Catching the exception is not enough. Without a deadline, one broken
        market holds the whole cycle open through the client's backoff ladder,
        and every other market's sampling cadence slips behind it -- which is
        the isolation this collector claims to provide."""

        async def slow(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(30)
            return httpx.Response(200, json=BOOK_A)

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(slow)),
            requests_per_second=10_000,
        )
        collector = MarketCollector(ticker=TICKER, client=client, poll_deadline_seconds=0.05)

        started = asyncio.get_running_loop().time()
        result = await collector.poll_once(session)
        elapsed = asyncio.get_running_loop().time() - started

        assert result.outcome is PollOutcome.FAILED
        assert elapsed < 5, f"poll ran {elapsed:.1f}s despite a 0.05s deadline"

    async def test_a_timed_out_poll_is_counted_not_silent(self, session: Session) -> None:
        async def slow(_request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(30)
            return httpx.Response(200, json=BOOK_A)

        client = KalshiRestClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(slow)),
            requests_per_second=10_000,
        )
        collector = MarketCollector(ticker=TICKER, client=client, poll_deadline_seconds=0.05)
        await collector.poll_once(session)

        assert collector.health.parse_errors == 1
        assert collector.health.messages == 0


class TestSubscriptionKey:
    def test_names_the_channel_and_the_market(self) -> None:
        """A later WebSocket stream of the same market must archive separately
        rather than interleaving two sampling regimes in one sequence space."""
        collector = collector_serving(BOOK_A)
        assert collector.subscription_key == f"orderbook_poll:{TICKER}"


class TestReplayEquivalence:
    async def test_archived_polls_replay_to_the_live_book(self, session: Session) -> None:
        """FR-001, end to end through the real collector and parser."""
        from arbbot.collection.replay import replay_archive
        from arbbot.venues.kalshi import KalshiAdapter

        collector = collector_serving(BOOK_A, BOOK_B)
        await collector.poll_once(session)
        await collector.poll_once(session)
        session.flush()

        live = collector.reconstructor.book.checksum()
        adapter = KalshiAdapter()
        replayed = replay_archive(
            session,
            venue=adapter.venue,
            subscription_key=collector.subscription_key,
            ticker=TICKER,
            decoder=lambda payload, sequence: adapter.decode_rest_orderbook(
                TICKER, payload, sequence or 0
            ),
        )

        assert replayed.checksum == live


@pytest.mark.parametrize("outcome", list(PollOutcome))
def test_every_outcome_is_documented(outcome: PollOutcome) -> None:
    assert outcome.__doc__
