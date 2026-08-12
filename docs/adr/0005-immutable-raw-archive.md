# ADR-0005: The raw archive is the source of truth

**Status:** Accepted · 2026-08-12

## Context

The system's central claim is falsifiable: that executable, fee-positive edge
exists often enough and large enough to matter. Testing that claim means
re-running today's analysis against months of past data after the parser, the
fee model, and the detector have all changed.

That is only possible if what was stored is what the venue actually sent,
rather than what some earlier version of the code believed it meant. A
normalized record is an interpretation, and interpretations go stale. When the
fee schedule turns out to have been misread, every past evaluation derived from
it is wrong — and the only way to recompute them is to still have the original
payloads.

NFR-03 (determinism) and NFR-05 (auditability) both bottom out here, as does
the go-live gate requiring 30 continuous days of replayable data.

## Decision

Persist every venue message verbatim in `raw_message`, with its receive time,
venue sequence number where one exists, SHA-256, and the parser schema version
in force at capture. Normalized records are derived artifacts and may be
rebuilt; raw payloads may not.

`raw_message` and `audit_event` are append-only, enforced by a PostgreSQL
trigger that raises on `UPDATE` and `DELETE`. Application-level discipline is
not sufficient: an accidental update to an archived payload invalidates every
replay derived from it, and the damage is silent. CI verifies the trigger
actually rejects both operations rather than trusting it was created.

Sequence numbers are stored exactly as received, including when they go
backwards. Gap detection depends on the archive being a faithful record of the
wire, not a cleaned-up one.

Duplicate suppression uses a content hash (`uq_raw_message_dedupe`), because
reconnects replay messages and the archive must not double-count them.

The audit log is hash-chained (`prev_hash` → `hash`) so that a removed or
altered row is detectable.

## Consequences

- Storage grows monotonically. This is acceptable and budgeted; it is the cost
  of being able to falsify a claim rather than assert it.
- Parser bugs are recoverable: fix the parser, replay the archive, recompute.
- A migration that needs to change `raw_message` must create a new table and
  copy, since the existing rows cannot be updated in place.
- Any personally identifying or licensed content in the archive is subject to
  the venue's data terms; redistribution requires review before commercial use.
