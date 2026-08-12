# Threat model

**Status:** Milestone 0 baseline · 2026-08-12

Scope: a single-operator research system collecting public market data, with a
later gated path to small supervised live trading. Revisit before Milestone 4
(demo credentials), before Milestone 5 (live credentials), and before any
commercial offering.

The assets worth protecting, in order: **the venue credential**, **the
integrity of the raw archive**, **the approval boundary**, and **the operator's
capital**. Note that confidentiality ranks below integrity here. This system's
research output is not especially secret; a corrupted archive or a bypassed
approval is what turns it into a machine for losing money.

## Trust boundaries

| Boundary | Trusted side | Untrusted side |
|---|---|---|
| Venue API / WebSocket | our parser | everything on the wire |
| Operator UI | authenticated human roles | anonymous network |
| AI coding agent | reviewed, merged code | agent-generated drafts |
| Configuration | validated `Settings` | environment, `.env`, CLI |
| Database | our schema and triggers | any client with the DSN |

## Threats and controls

### T1 — Venue credential disclosure

*A live API key leaks through source control, logs, a screenshot, or a prompt
pasted into a chat.*

- Separate credentials per environment; live keys exist only in
  `live_supervised` / `live_bounded` deployments.
- The config layer **rejects** a credential in `local` and `research`
  environments, so a live key cannot sit unnoticed in a research deployment.
- Credentials are `SecretStr`; `repr()` and `str()` do not render them, and a
  test asserts this.
- `.env`, `*.pem`, `*.key`, and `secrets/` are gitignored; gitleaks scans full
  history in CI.
- **Residual:** a determined operator can still paste a key somewhere. Mitigated
  by least-privilege keys, rotation, and an emergency revocation procedure that
  must exist before Milestone 5.

### T2 — Archive tampering or silent corruption

*An archived payload is altered, so every replay derived from it is wrong and
nothing indicates why.*

- `raw_message` and `audit_event` are append-only via database trigger, not
  convention. CI verifies `UPDATE` and `DELETE` are actually rejected.
- Every payload carries a SHA-256 and its capture-time schema version.
- The audit log is hash-chained, so a removed row is detectable.
- **Residual:** a superuser can drop the trigger. Detection depends on the hash
  chain and on backups; database superuser access must be restricted before any
  live deployment.

### T3 — Approval boundary bypass

*Something that is not an authenticated human approves a relationship or an
order — an AI agent, a service account, a misrouted API call.*

- Relationships enter as `PENDING` and cannot qualify a candidate.
- Approval endpoints require authenticated human roles and are never
  model-callable in production.
- Live submission requires three independent gates (ADR-0004), one of which is
  a compile-time constant that cannot be set by environment variable.
- The state machine forbids `PROPOSED → SUBMITTING`; only the risk engine can
  produce `RISK_APPROVED`.
- **Residual:** an operator can approve carelessly. Mitigated by requiring
  recorded evidence per approval and two-person approval before Milestone 6.

### T4 — Malicious or malformed venue input

*A payload is crafted or simply unexpected, and the parser crashes, misreads a
price, or is used to reach the database.*

- Pydantic validation at the boundary; parametrised SQL only.
- Prices are validated to 1–99 cents; a leg at 0 or 100 is rejected rather than
  treated as free money.
- `float` cannot enter the money path (ADR-0003).
- Sequence gaps mark the book incomplete and suspend evaluation rather than
  interpolating.
- **Residual:** a venue schema change can silently alter meaning without
  altering shape. Terms hashing and the `unknown_fee` rejection are the backstop.

### T5 — Supply chain compromise

*A dependency ships malicious code into a process that will eventually hold API
credentials.*

- Dependencies pinned to compatible ranges in `pyproject.toml`; `pip-audit`
  runs in CI.
- The container runs as a non-root user and does not write to its own image.
- **Residual:** a lockfile with hashes is not yet in place. Required before
  Milestone 5 — a system that can place orders should not resolve dependencies
  at deploy time.

### T6 — Runaway or duplicated execution

*A retry, a restart, or a race submits an order twice, or a bug submits many.*

- Not reachable at Milestone 0: no execution path is compiled in.
- From Milestone 4: idempotency key per intent, reused across retries; a
  `duplicate_intent` rejection catches repeats.
- `UNKNOWN` is treated as exposure and may only move to `RECONCILING` — an
  uncertain venue response is never retried directly, because assuming a leg
  did not fill and re-sending it is how one position becomes two.
- Hard caps on per-leg notional, unmatched exposure, total exposure, and daily
  loss; global and per-strategy kill switches from Milestone 4.

### T7 — Operator error in deployment

*The wrong environment, the wrong database, or a flag set in the wrong place.*

- `Settings` validation refuses incoherent combinations at startup rather than
  running in a subtly wrong state.
- `arbbot doctor` prints both standing gates, the environment, and the risk
  limits, so "what is this deployment allowed to do" has a one-command answer.
- Alembic reads its URL from `arbbot.config`, never from `alembic.ini`, so a
  migration cannot target a different database than the application.
- CI asserts a built image ships with both build flags off.

## Out of scope at Milestone 0

Multi-tenant access control, customer data, key management infrastructure,
network segmentation, and DDoS resilience. None apply to a single-operator
research deployment on public endpoints. All require review before any
customer-facing offering.
