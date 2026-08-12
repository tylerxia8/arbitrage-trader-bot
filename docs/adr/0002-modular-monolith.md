# ADR-0002: Modular monolith, not microservices

**Status:** Accepted · 2026-08-12

## Context

The system has clearly separable components — venue adapter, normalizer,
relationship registry, fee service, detector, shadow executor, risk engine,
ledger/reconciler, reporter. That separation invites splitting them into
services.

It should not, at this stage. The system has one operator, runs on one venue,
and its hardest requirement is determinism: the same inputs, configuration, and
rule versions must produce the same decision (NFR-03). Distributing the
components across process boundaries buys independent scaling nobody needs and
costs exactly the property the project depends on — a decision assembled from
several services is a decision whose inputs arrived in a nondeterministic
order, and reproducing it means reproducing the interleaving.

There is also a correctness argument. The risk engine must be the only
authoriser of an intent. Inside one process that is a function call that cannot
be bypassed. Across a network it is a protocol that can be bypassed by anything
that can reach the execution adapter's port.

## Decision

Build a modular monolith: one Python service, separate asynchronous workers for
ingestion, normalization, detection, shadow execution, reporting, and
reconciliation, with well-defined internal interfaces between them.

Module boundaries are enforced by import discipline and reviewed in pull
requests. Components communicate through the database and in-process
interfaces, not over HTTP.

PostgreSQL is the persistent store. SQLite is permitted for local fixtures
only, and the config layer rejects any other backend.

## Consequences

- One process to run, one log stream to read, one transaction boundary around a
  decision and the evidence for it.
- Replay is straightforward: feed the raw archive through the same code path.
- Scaling limits are real but distant; a single venue's feed does not saturate
  one process.
- If a component genuinely needs to scale independently later, extracting it is
  a refactor. That is the cheaper direction to move in.
