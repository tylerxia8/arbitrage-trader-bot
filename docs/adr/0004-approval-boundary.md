# ADR-0004: The approval boundary and the three-gate live path

**Status:** Accepted · 2026-08-12

## Context

Two requirements constrain what this system may do on its own.

FR-005 requires human approval metadata before a relationship can qualify a
candidate. An AI agent may draft a mapping — "these three outcomes are mutually
exclusive and collectively exhaustive" — but a drafted mapping is a claim about
settlement wording, not a fact. If the claim is wrong, the "guaranteed" payout
is not guaranteed, and the system has bought a directional position while
believing it holds a hedged one. That is the failure mode with no upper bound
on its cost, and it is invisible until settlement.

FR-016 requires live order submission to sit behind a build flag, a runtime
flag, and manual per-basket approval, such that any missing gate makes an order
impossible.

Both point at the same principle: the money path must be deterministic code,
and the decisions that determine whether a payout is truly guaranteed must be
made by a person who read the terms.

## Decision

**Approval boundary.** A relationship enters the registry as `PENDING` and can
never qualify a candidate in that state. Approval records the reviewer, the
timestamp, the evidence they read, the scope, and the terms hash of every leg
at the time of signing. Approval is granted by an authenticated human role;
`POST /relationships/{id}/approve` is never model-callable in production.

Any change to a leg's material terms hash suspends the relationship
automatically (FR-004) and requires re-approval. The registry re-checks
dependency hashes on every evaluation rather than trusting that the legs are
still what the reviewer read.

**Three gates.** Live submission requires all of:

1. `buildflags.LIVE_EXECUTION_COMPILED_IN` — a source constant, deliberately
   not readable from the environment. Flipping it requires a pull request and a
   release, which is reviewable and permanently recorded in version control.
2. `ARBBOT_LIVE_TRADING_ENABLED` — the runtime flag, valid only in a
   `live_supervised` or `live_bounded` environment.
3. An unexpired, in-bounds human approval for the specific basket.

`Settings.may_submit_live_orders()` reports gates 1 and 2 only. It is documented
as necessary and never sufficient; gate 3 is checked at execution time.

A configuration that sets the runtime flag on a build without the live path
compiled in refuses to start. That combination means someone misunderstands
what is deployed, and failing at startup surfaces it immediately rather than at
the moment an order is expected to fire and silently does not.

**Authorisation chain.** The detector may recommend an intent. Only the risk
engine may authorise one. Only the execution adapter may submit it. The state
machine encodes this: `PROPOSED` cannot reach `SUBMITTING` without passing
through `RISK_APPROVED`.

## Consequences

- Milestone 0–4 builds cannot place a live order regardless of configuration.
- Arming the system is a code change, not a deployment setting, and shows up in
  `git log`.
- An AI agent can draft relationships and open pull requests, and cannot approve
  its own drafts or submit an order.
- Two-person approval for automated live use is a policy layer on top of this
  and is required before Milestone 6.
