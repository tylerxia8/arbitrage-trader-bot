"""Polling collector.

Streaming would give finer time resolution, but Kalshi requires a credential on
the WebSocket handshake even for public channels, and Milestone 1 runs before
any credential is authorised (see ``docs/venue-findings.md``). Polling the
public REST orderbook is the credential-free path to continuous collection.

Three decisions here are worth stating plainly, because each trades something
away.

**Every poll is a full snapshot.** REST has no deltas, so reconstruction never
has to survive a gap -- but it also means the book between two polls is
unobserved. An opportunity shorter than the poll interval is invisible to this
collector. That biases the Milestone 3 duration measurement toward "no edge",
which is the safe direction to be wrong in, but it is still wrong and must be
read that way.

**Sequence numbers are local.** REST supplies none, so the collector assigns a
monotonic counter per stream. It resumes from the highest sequence already
archived rather than restarting at zero, because a restart that reused numbers
would collide with the archive's identity constraint -- and over a seven-day
run, restarts happen.

**Unchanged books are not re-archived.** A poll that returns a byte-identical
book is real information, but storing thousands of identical snapshots would
bloat the archive without adding evidence. The poll is still counted in
``feed_health``, so liveness stays visible: "we polled and nothing changed" and
"we stopped polling" remain distinguishable, which is the property that
actually matters.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arbbot.collection.archive import RawArchive, canonical_hash
from arbbot.collection.health import StreamHealth
from arbbot.db.models import BookSnapshot, RawMessage
from arbbot.marketdata.reconstruct import BookReconstructor
from arbbot.marketdata.types import BookSide
from arbbot.venues.kalshi import KalshiAdapter
from arbbot.venues.kalshi.rest import KalshiRestClient

__all__ = ["CHANNEL", "MarketCollector", "PollOutcome", "PollResult"]

#: Logical channel name for the polled orderbook. Part of the subscription key,
#: so a future WebSocket stream of the same market archives separately rather
#: than interleaving two different sampling regimes into one sequence space.
CHANNEL = "orderbook_poll"


class PollOutcome(enum.StrEnum):
    """What one poll produced."""

    STORED = "stored"
    """The book changed and was archived."""

    UNCHANGED = "unchanged"
    """Byte-identical to the previous poll; counted, not re-archived."""

    FAILED = "failed"
    """The request or the parse failed. Counted as a parse error or a gap in
    liveness, never as an empty book -- an empty book is a tradeable claim."""


@dataclass(slots=True)
class PollResult:
    outcome: PollOutcome
    ticker: str
    sequence: int | None = None
    checksum: str | None = None
    error: str | None = None


@dataclass(slots=True)
class MarketCollector:
    """Collects one market's orderbook by polling."""

    ticker: str
    client: KalshiRestClient
    adapter: KalshiAdapter = field(default_factory=KalshiAdapter)

    poll_deadline_seconds: float | None = 30.0
    """Hard ceiling on one poll, retries included.

    Without it, the client's backoff ladder can spend half a minute on a
    single broken market, and every other market in the cycle waits behind it.
    The isolation this collector claims -- one bad ticker must not stop the
    run -- is not real unless a bad ticker is also bounded in *time*, not just
    caught as an exception.
    """
    archive: RawArchive = field(init=False)
    reconstructor: BookReconstructor = field(init=False)
    health: StreamHealth = field(init=False)

    _sequence: int = field(init=False, default=0)
    _last_hash: str | None = field(init=False, default=None)
    _resumed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.archive = RawArchive(
            venue=self.adapter.venue, schema_version=self.adapter.schema_version
        )
        self.reconstructor = BookReconstructor(self.ticker)
        self.health = StreamHealth(venue=self.adapter.venue, subscription_key=self.subscription_key)

    @property
    def subscription_key(self) -> str:
        return self.adapter.subscription_key(CHANNEL, self.ticker)

    def resume(self, session: Session) -> int:
        """Continue this stream's sequence from what is already archived.

        Restarting at zero would collide with existing rows on the archive's
        ``(venue, subscription_key, sequence)`` identity, and the collector
        would silently stop storing anything it had already seen a number for.
        """
        highest = session.execute(
            select(func.max(RawMessage.sequence)).where(
                RawMessage.venue == self.adapter.venue,
                RawMessage.subscription_key == self.subscription_key,
            )
        ).scalar()
        self._sequence = int(highest or 0)
        self._resumed = True
        return self._sequence

    async def poll_once(self, session: Session, *, now: dt.datetime | None = None) -> PollResult:
        """Fetch, archive, reconstruct, and snapshot one market."""
        if not self._resumed:
            self.resume(session)

        try:
            if self.poll_deadline_seconds is None:
                fetched = await self.client.fetch_orderbook(self.ticker)
            else:
                async with asyncio.timeout(self.poll_deadline_seconds):
                    fetched = await self.client.fetch_orderbook(self.ticker)
        except Exception as exc:
            self.health.observe_parse_error()
            return PollResult(PollOutcome.FAILED, self.ticker, error=f"{type(exc).__name__}: {exc}")

        received = now or fetched.received_ts
        self.health.observe_message(received)

        digest = canonical_hash(fetched.payload)
        if digest == self._last_hash:
            return PollResult(PollOutcome.UNCHANGED, self.ticker, checksum=digest)

        self._sequence += 1
        sequence = self._sequence

        try:
            event = self.adapter.decode_rest_orderbook(self.ticker, fetched.payload, sequence)
        except Exception as exc:
            self.health.observe_parse_error()
            self.reconstructor.book.invalidate()
            return PollResult(PollOutcome.FAILED, self.ticker, error=f"{type(exc).__name__}: {exc}")

        archived = self.archive.record(
            session,
            channel=CHANNEL,
            payload=fetched.payload,
            subscription_key=self.subscription_key,
            sequence=sequence,
            received_ts=received,
        )
        # Flush now so the snapshot can reference the archived row: a snapshot
        # that cannot name the payload it came from is not traceable evidence.
        session.flush()

        self.reconstructor.apply(event)
        self.health.tracker.observe(sequence)
        self._last_hash = digest

        session.add(self._snapshot_row(sequence, received, archived.row))
        return PollResult(
            PollOutcome.STORED,
            self.ticker,
            sequence=sequence,
            checksum=self.reconstructor.book.checksum(),
        )

    def _snapshot_row(
        self, sequence: int, captured: dt.datetime, source: RawMessage | None
    ) -> BookSnapshot:
        levels = self.reconstructor.book.levels_by_side()
        return BookSnapshot(
            venue=self.adapter.venue,
            ticker=self.ticker,
            captured_ts=captured,
            sequence=sequence,
            # Decimal keys and values are stringified for JSON: a float round
            # trip here would undo the exactness the whole money path protects.
            yes_levels={str(p): str(q) for p, q in levels[BookSide.YES].items()},
            no_levels={str(p): str(q) for p, q in levels[BookSide.NO].items()},
            checksum=self.reconstructor.book.checksum(),
            is_complete=self.reconstructor.book.is_complete,
            raw_message_id=source.id if source is not None else None,
        )
