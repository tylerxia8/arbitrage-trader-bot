# Arbitrage Trader Bot

Research platform for detecting and validating **logical arbitrage** in regulated
event-contract markets, starting with Kalshi.

This is an *evidence engine first*. It collects market data, evaluates only
human-approved logical relationships, and simulates execution against real
order-book depth and exact fees. Whether a tradeable edge exists is a question
this system is built to answer — not an assumption it is built on.

> **Status: pre-M0.** Read-only and shadow-simulation only.
> Live order submission is not implemented and is gated behind a build flag, a
> runtime flag, and per-basket human approval.

## Scope

**In scope (v1)** — same-venue logical relationships on binary contracts:

- **Exhaustive baskets** — buy YES across a mutually exclusive, collectively
  exhaustive outcome set; guaranteed minimum payout of $1 per set.
- **Implication pairs** — where A ⇒ B, hold NO A + YES B.
- **Interval partitions** — non-overlapping intervals covering every result.

**Out of scope (v1)** — cross-venue arbitrage, sportsbooks, equities, FX,
futures, options, bonds, crypto, high-frequency market making, trading anyone
else's capital, and automated performance marketing.

## Design rules

These are release blockers, not preferences:

| Rule | Rationale |
|---|---|
| Decimal / fixed-point only in the money path | Float rounding silently corrupts edge calculations |
| Unknown fees never default to zero | A missing fee rule must reject the candidate, not inflate it |
| Detector proposes → risk engine authorizes → adapter submits | No component may bypass this chain |
| AI may draft relationships; only a human approves them | Model output can never authorize capital at risk |
| Fail closed | Stale data, sequence gaps, or changed terms halt evaluation |
| Executable prices only | Depth-weighted asks/bids — never midpoint or last trade |
| Everything replayable | Any decision must reproduce from immutable stored inputs |

## Milestones

| | Deliverable | Exit gate |
|---|---|---|
| **M0** | Repo, CI, schemas, config, threat model | Tests pass; no secrets; reproducible setup |
| **M1** | REST/WebSocket ingestion, raw archive, health metrics | 7 days continuous collection; replay works |
| **M2** | Relationship registry, detectors, fee service | Golden cases pass; every rejection explained |
| **M3** | Shadow execution, capital ledger, opportunity lifecycle | 30-day falsification report; stress-positive **or the project pauses** |
| **M4** | Demo integration, order state machine, reconciliation | 100 demo baskets; 0 unexplained ledger differences |
| **M5** | Supervised tiny-live, capped | 25–100 reconciled baskets; positive aggregate net P&L |
| **M6** | Bounded autonomy within a narrow whitelist | Independent review, proven profitability, kill-switch drills |

Milestones ship in order. Each exit gate is a checkpoint, not a formality.

## Planned stack

Python 3.12 · asyncio/httpx · Pydantic · PostgreSQL · SQLAlchemy/Alembic ·
FastAPI · OpenTelemetry · Docker Compose · pytest · Hypothesis · Ruff · mypy

## Disclaimer

Personal research project, built with personal capital. Nothing here is legal,
tax, investment, or gambling advice, and no part of it is an offer to trade on
anyone else's behalf. Prediction-market regulation and venue terms change;
verify current documentation, fee schedules, and applicable law independently.
No claim is made that this system is or will be profitable.

## License

MIT — see [LICENSE](LICENSE).
