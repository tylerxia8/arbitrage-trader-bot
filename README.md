# Arbitrage Trader Bot

Research platform for detecting and validating **logical arbitrage** in regulated
event-contract markets, starting with Kalshi.

This is an *evidence engine first*. It collects market data, evaluates only
human-approved logical relationships, and simulates execution against real
order-book depth and exact fees. Whether a tradeable edge exists is a question
this system is built to answer â€” not an assumption it is built on.

> **Status: M1 in progress.** Read-only and shadow-simulation only.
> Live order submission is not implemented and is gated behind a build flag, a
> runtime flag, and per-basket human approval.

## Scope

**In scope (v1)** â€” same-venue logical relationships on binary contracts:

- **Exhaustive baskets** â€” buy YES across a mutually exclusive, collectively
  exhaustive outcome set; guaranteed minimum payout of $1 per set.
- **Implication pairs** â€” where A â‡’ B, hold NO A + YES B.
- **Interval partitions** â€” non-overlapping intervals covering every result.

**Out of scope (v1)** â€” cross-venue arbitrage, sportsbooks, equities, FX,
futures, options, bonds, crypto, high-frequency market making, trading anyone
else's capital, and automated performance marketing.

## Design rules

These are release blockers, not preferences:

| Rule | Rationale |
|---|---|
| Decimal / fixed-point only in the money path | Float rounding silently corrupts edge calculations |
| Unknown fees never default to zero | A missing fee rule must reject the candidate, not inflate it |
| Detector proposes â†’ risk engine authorizes â†’ adapter submits | No component may bypass this chain |
| AI may draft relationships; only a human approves them | Model output can never authorize capital at risk |
| Fail closed | Stale data, sequence gaps, or changed terms halt evaluation |
| Executable prices only | Depth-weighted asks/bids â€” never midpoint or last trade |
| Everything replayable | Any decision must reproduce from immutable stored inputs |

## Milestones

| | Deliverable | Exit gate |
|---|---|---|
| **M0** | Repo, CI, schemas, config, threat model | Tests pass; no secrets; reproducible setup |
| **M1** | REST/WebSocket ingestion, raw archive, health metrics | 7 days continuous collection; replay works |
| **M2** | Relationship registry, detectors, fee service | Golden cases pass; every rejection explained |
| **M3** | Shadow execution, capital ledger, opportunity lifecycle | 30-day falsification report; stress-positive **or the project pauses** |
| **M4** | Demo integration, order state machine, reconciliation | 100 demo baskets; 0 unexplained ledger differences |
| **M5** | Supervised tiny-live, capped | 25â€“100 reconciled baskets; positive aggregate net P&L |
| **M6** | Bounded autonomy within a narrow whitelist | Independent review, proven profitability, kill-switch drills |

Milestones ship in order. Each exit gate is a checkpoint, not a formality.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) â€” it provisions Python 3.12 itself,
so no system Python is needed. Docker is optional, for PostgreSQL.

```bash
make setup          # or:  .\scripts\dev.ps1 setup
make check          # or:  .\scripts\dev.ps1 check    -- lint, types, FR-002, tests
```

`make check` runs everything CI runs and needs no database. For migrations:

```bash
make up             # start PostgreSQL
cp .env.example .env
make migrate        # alembic upgrade head
arbbot doctor       # what is this deployment allowed to do?
```

`arbbot doctor` prints both standing execution gates, the environment, and the
risk limits. It is the one-command answer to "is this thing armed" â€” and at
Milestone 0 the answer is always no, because the live path is not compiled in.

## Layout

| Path | Contents |
|---|---|
| [src/arbbot/money.py](src/arbbot/money.py) | Fixed-point primitives; conservative rounding |
| [src/arbbot/config.py](src/arbbot/config.py) | Validated settings, risk limits, live gates |
| [src/arbbot/buildflags.py](src/arbbot/buildflags.py) | Compile-time capability flags (not env-readable) |
| [src/arbbot/states.py](src/arbbot/states.py) | Order intent state machine |
| [src/arbbot/reasons.py](src/arbbot/reasons.py) | Closed rejection-reason catalog |
| [src/arbbot/marketdata/](src/arbbot/marketdata/) | Order book, sequencing, reconstruction |
| [src/arbbot/collection/](src/arbbot/collection/) | Raw archive, feed health, deterministic replay |
| [src/arbbot/venues/](src/arbbot/venues/) | Venue adapter boundary |
| [src/arbbot/db/](src/arbbot/db/) | Models: raw archive, markets, terms, registry, audit |
| [tools/check_no_float.py](tools/check_no_float.py) | FR-002 static enforcement |
| [docs/adr/](docs/adr/) | Architecture decision records |
| [src/arbbot/venues/kalshi/](src/arbbot/venues/kalshi/) | Kalshi adapter: parser + public REST client |
| [docs/venue-findings.md](docs/venue-findings.md) | Verified venue behaviour and open decisions |
| [docs/threat-model.md](docs/threat-model.md) | Threat model |

## Stack

Python 3.12 Â· asyncio/httpx Â· Pydantic Â· PostgreSQL Â· SQLAlchemy/Alembic Â·
FastAPI Â· Docker Compose Â· pytest Â· Hypothesis Â· Ruff Â· mypy (strict)

## Disclaimer

Personal research project, built with personal capital. Nothing here is legal,
tax, investment, or gambling advice, and no part of it is an offer to trade on
anyone else's behalf. Prediction-market regulation and venue terms change;
verify current documentation, fee schedules, and applicable law independently.
No claim is made that this system is or will be profitable.

## License

MIT â€” see [LICENSE](LICENSE).
