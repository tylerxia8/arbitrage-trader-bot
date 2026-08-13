"""The raw archive: a faithful record of what the venue actually sent.

Every replay, every falsification report, and every audit trail bottoms out
here (ADR-0005). Two properties are load-bearing.

**Fidelity.** The payload is stored exactly as received. The hash is computed
over a canonical JSON encoding so that the same logical message always hashes
identically regardless of key order, which makes the fingerprint useful for
integrity checks rather than merely decorative.

**Honest deduplication.** Reconnects re-deliver messages, and storing them
twice would double-count them on replay. But the archive must only suppress
messages it *knows* are re-deliveries. Two heartbeats a minute apart have
identical payloads and are both real events; suppressing the second would be
data loss disguised as hygiene. So deduplication keys on stream identity plus
sequence number -- the venue's own statement of "this is message N of this
subscription" -- and messages without a sequence are always stored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from arbbot.db.models import RawMessage

__all__ = ["ArchivedMessage", "RawArchive", "canonical_hash"]


def canonical_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over a canonical JSON encoding of ``payload``.

    Sorted keys and tight separators mean the hash depends on content alone,
    not on how the venue's serialiser happened to order a dictionary that day.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchivedMessage:
    """Outcome of an archive write."""

    row: RawMessage | None
    was_duplicate: bool

    @property
    def stored(self) -> bool:
        return self.row is not None


class RawArchive:
    """Append-only writer for :class:`~arbbot.db.models.RawMessage`."""

    def __init__(self, venue: str, schema_version: str) -> None:
        self.venue = venue
        self.schema_version = schema_version
        """Parser contract in force at capture. Stored per message so that a
        later parser change cannot silently reinterpret old payloads."""

    def record(
        self,
        session: Session,
        *,
        channel: str,
        payload: dict[str, Any],
        subscription_key: str | None = None,
        sequence: int | None = None,
        source_ts: dt.datetime | None = None,
        received_ts: dt.datetime | None = None,
    ) -> ArchivedMessage:
        """Persist one venue message.

        :param subscription_key: identity of the stream the sequence belongs
            to, e.g. ``"orderbook_delta:KXBTC-25DEC31"``. Sequence numbers are
            per-subscription, so without this a message from one market would
            collide with the same sequence number from another.
        :returns: the stored row, or a duplicate marker if this exact
            ``(stream, sequence)`` is already archived.
        """
        if sequence is not None and subscription_key is None:
            raise ValueError(
                "a sequenced message must carry a subscription_key; without one its "
                "sequence number cannot be told apart from another market's"
            )

        if sequence is not None and self._already_archived(session, subscription_key, sequence):
            return ArchivedMessage(row=None, was_duplicate=True)

        row = RawMessage(
            venue=self.venue,
            channel=channel,
            subscription_key=subscription_key,
            sequence=sequence,
            source_ts=source_ts,
            payload=payload,
            sha256=canonical_hash(payload),
            schema_version=self.schema_version,
        )
        if received_ts is not None:
            row.received_ts = received_ts
        session.add(row)
        return ArchivedMessage(row=row, was_duplicate=False)

    def _already_archived(
        self, session: Session, subscription_key: str | None, sequence: int
    ) -> bool:
        stmt = select(RawMessage.id).where(
            RawMessage.venue == self.venue,
            RawMessage.subscription_key == subscription_key,
            RawMessage.sequence == sequence,
        )
        return session.execute(stmt).first() is not None
