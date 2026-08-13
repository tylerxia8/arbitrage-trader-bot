# The seven-day collection run

Operating notes for the Milestone 1 exit gate: *7 days continuous collection;
replay works*.

## Start it

```bash
docker compose up -d db                    # PostgreSQL on 55432
docker compose run --rm app alembic upgrade head
docker compose --profile collect up -d     # the collector, supervised
```

The collector is behind a Compose profile so an ordinary `docker compose up`
during development does not quietly start collecting against the live venue.

## Check on it

```bash
docker compose --profile collect logs -f collector   # what it is doing
arbbot coverage                                      # are we there yet
```

`arbbot coverage` exits non-zero until the gate is met, so it can gate a
release rather than needing someone to read a table and decide.

## What it collects

`--universe` resolves live temperature partitions from the venue and refreshes
them every 15 minutes. It does **not** use a fixed ticker list, because daily
markets rotate: yesterday's Atlanta event already has zero active markets, and
a fixed list would spend the week polling settled contracts while reporting
itself perfectly healthy.

Only structurally complete partitions are collected — numeric buckets with
both tails, verified to tile the integers. Merely mutually-exclusive events are
excluded; see `docs/market-selection.md` for what that mistake looks like
priced.

Roughly 120 markets across ~20 temperature series, polled every 30 seconds.
That is about 15 seconds of venue traffic per cycle at the client's
self-imposed 8 requests/second, comfortably inside both the interval and the
venue's ~20/second read budget.

## What could still interrupt it

| Risk | Mitigation | Residual |
|---|---|---|
| Machine sleeps | Sleep disabled on AC (`standby-timeout-ac 0`) | **Only on AC.** On battery it still sleeps after 5 minutes, deliberately — an unplugged machine should protect itself. Keep it plugged in. |
| Collector crashes | `restart: unless-stopped`; sequences resume from the archive | A crash loop would still leave holes; `arbbot coverage` reports them |
| Docker Desktop restarts | Enable "Start Docker Desktop when you log in" | A Windows Update reboot still needs a login to resume |
| Venue outage | Retries with backoff; a failed poll is counted, never recorded as an empty book | A long outage is a real hole, and is reported as one |
| Disk fills | Unchanged books are not re-archived; logs capped at 100 MB | Growth is modest but unbounded over months |

**Windows Update is the most likely thing to break a seven-day run.** A forced
restart outside active hours will stop everything until someone logs back in.
Worth setting active hours wide, or pausing updates for the week.

## Reading the result

The gate measures *collector* continuity — that something was always being
collected — not that any one market lasted a week. Daily markets exist for
about a day, so per-stream continuity is unsatisfiable by construction.

Reported separately: streams that went quiet **while still listed**. That is a
market whose book stopped updating before it settled, and it is where a basket
loses a leg. It does not fail the gate, but it is the number to look at before
trusting any candidate built on that market.

## Afterwards

Stop the collector, then verify the other half of the gate — replay:

```bash
docker compose --profile collect down
arbbot coverage        # should exit 0
```

Coverage measures the archive, not the present, so stopping the collector does
not retract the week. Replay equivalence is exercised continuously by the test
suite; re-running it against the real archive is the M2 starting point.
