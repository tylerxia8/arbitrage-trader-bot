"""Deterministic replay from the raw archive.

FR-001's acceptance criterion is that raw and normalized records replay to an
identical book state. This is the machinery that demonstrates it: read the
archived payloads back in wire order, decode them with the parser version of
the caller's choosing, and drive the same
:class:`~arbbot.marketdata.reconstruct.BookReconstructor` that live collection
drives.

Two uses follow from that. Routine: rebuild a book without keeping every
market in memory forever. Consequential: when the parser or the fee model
turns out to have been wrong, re-derive every past decision from what the
venue actually sent rather than from what an older parser believed it meant.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.db.models import RawMessage
from arbbot.marketdata.reconstruct import BookReconstructor, ReconstructionStats
from arbbot.marketdata.types import BookEvent

__all__ = ["BookEventDecoder", "ReplayResult", "replay_archive", "replay_events"]

#: Turns one archived payload into a book event, or ``None`` if the payload is
#: not a book message (a heartbeat, a status update, an unrelated channel).
#: Raising instead of returning ``None`` signals a payload that *should* have
#: decoded and did not, which is counted as a decode failure.
#: Receives the archived sequence number alongside the payload. Wire formats
#: that carry their own sequence ignore it; polled REST snapshots depend on it,
#: because that sequence is assigned by the collector and stored in the archive
#: row rather than inside the venue payload -- which must stay exactly as the
#: venue sent it.
BookEventDecoder = Callable[[dict[str, Any], int | None], BookEvent | None]


@dataclass(slots=True)
class ReplayResult:
    """Outcome of replaying one market's stream."""

    ticker: str
    checksum: str
    is_complete: bool
    final_sequence: int | None
    messages_read: int = 0
    events_decoded: int = 0
    decode_failures: int = 0
    stats: ReconstructionStats = field(default_factory=ReconstructionStats)

    @property
    def is_faithful(self) -> bool:
        """Whether this replay reconstructed a usable book with no losses.

        A replay that ends incomplete, or that could not decode a payload it
        was given, has not demonstrated FR-001 -- it has demonstrated that
        something is wrong with the archive or the parser.
        """
        return self.is_complete and self.decode_failures == 0 and self.stats.rejected == 0

    def matches(self, other: ReplayResult | str) -> bool:
        """Whether this reconstructed the same book state as ``other``.

        Compares checksums rather than objects: state equality is the claim,
        and a checksum makes it exact and cheap to store next to a snapshot.
        """
        expected = other if isinstance(other, str) else other.checksum
        return self.checksum == expected


def replay_events(ticker: str, events: Iterable[BookEvent]) -> ReplayResult:
    """Replay decoded events. The pure core, with no database involved."""
    reconstructor = BookReconstructor(ticker)
    decoded = 0
    for event in events:
        decoded += 1
        reconstructor.apply(event)

    return ReplayResult(
        ticker=ticker,
        checksum=reconstructor.book.checksum(),
        is_complete=reconstructor.book.is_complete,
        final_sequence=reconstructor.book.sequence,
        messages_read=decoded,
        events_decoded=decoded,
        stats=reconstructor.stats,
    )


def replay_archive(
    session: Session,
    *,
    venue: str,
    subscription_key: str,
    ticker: str,
    decoder: BookEventDecoder,
    schema_version: str | None = None,
) -> ReplayResult:
    """Replay one subscription's archived messages.

    :param schema_version: replay only payloads captured under this parser
        contract. Leave ``None`` to replay every message regardless, which is
        the right choice when re-deriving history with a *new* parser.
    """
    reconstructor = BookReconstructor(ticker)
    read = decoded = failures = 0

    for payload, sequence in _archived_payloads(session, venue, subscription_key, schema_version):
        read += 1
        try:
            event = decoder(payload, sequence)
        except Exception:
            failures += 1
            continue
        if event is None:
            continue
        decoded += 1
        reconstructor.apply(event)

    return ReplayResult(
        ticker=ticker,
        checksum=reconstructor.book.checksum(),
        is_complete=reconstructor.book.is_complete,
        final_sequence=reconstructor.book.sequence,
        messages_read=read,
        events_decoded=decoded,
        decode_failures=failures,
        stats=reconstructor.stats,
    )


def _archived_payloads(
    session: Session,
    venue: str,
    subscription_key: str,
    schema_version: str | None,
) -> Iterator[tuple[dict[str, Any], int | None]]:
    """Stream archived payloads, with their sequence numbers, in wire order.

    Ordered by primary key, which is insertion order, which is the order the
    messages arrived. Ordering by ``received_ts`` would be wrong: the column
    has limited resolution, and two messages sharing a timestamp would replay
    in an arbitrary order -- reconstructing a different book on each run and
    quietly destroying determinism.

    Yielded lazily so that replaying months of archive does not require
    holding months of archive in memory.
    """
    stmt = (
        select(RawMessage.payload, RawMessage.sequence)
        .where(
            RawMessage.venue == venue,
            RawMessage.subscription_key == subscription_key,
        )
        .order_by(RawMessage.id)
    )
    if schema_version is not None:
        stmt = stmt.where(RawMessage.schema_version == schema_version)

    for row in session.execute(stmt).yield_per(1000):
        yield row.payload, row.sequence
