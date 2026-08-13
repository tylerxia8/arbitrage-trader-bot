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

## Book age, and what this archive can and cannot support

Measured on the live run: **worst observed book age 15.2 seconds**, against a
30-second poll interval. That is arithmetic, not a fault — 120 markets at the
client's 8 requests/second is a 15-second cycle, so the first market polled is
15 seconds old by the time the cycle ends, and any book is 0–30 seconds old at
an arbitrary moment.

It has a consequence worth stating before anyone reads a result off this
archive. The configured staleness threshold is `max_quote_age_ms = 2000`. A
detector applying it to this data would reject **every** evaluation, correctly:
a fifteen-second-old book is not evidence that a price was executable.

So this archive can answer *"do priced inconsistencies appear, and how often"*.
It cannot, on its own, answer *"could one have been executed"*. That second
question needs either the WebSocket feed — which requires a credential, still
an open owner decision — or an explicit acknowledgement that the M3 capture
estimate is an upper bound built on stale quotes.

Do not resolve this by raising the staleness threshold to fit the data. The
threshold describes what execution requires; loosening it to make polled data
qualify would produce candidates that look executable and are not.

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
