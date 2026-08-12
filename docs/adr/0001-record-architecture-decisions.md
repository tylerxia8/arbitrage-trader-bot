# ADR-0001: Record architecture decisions

**Status:** Accepted · 2026-08-12

## Context

The specification requires an architecture decision record for every material
deviation from the PRD, and requires that decisions be auditable after the
fact. It also anticipates that most code will be written by an AI coding agent
working milestone by milestone.

That combination makes undocumented decisions especially costly. An agent
picking up Milestone 2 has no memory of why Milestone 0 chose what it chose,
and will re-litigate settled questions or, worse, quietly reverse them. A
constraint that exists only in someone's head is a constraint that will be
violated by the next contributor, human or otherwise.

## Decision

Record every material decision as a numbered ADR in `docs/adr/`, using the
Status / Context / Decision / Consequences structure.

An ADR is required when a change alters the money path, the approval boundary,
what the system is permitted to do without a human, the data retained for
replay, or any behaviour the PRD marks MUST. Ordinary implementation choices do
not need one.

ADRs are immutable once accepted. A reversal is a new ADR that supersedes the
old one, and the old one is marked Superseded rather than edited — the reasoning
that was true at the time is part of the record.

## Consequences

- The reasoning behind a constraint survives the context window that produced it.
- Reviewers can check a pull request against a written decision instead of
  inferring intent.
- Small friction on genuinely material changes, which is the intent.
