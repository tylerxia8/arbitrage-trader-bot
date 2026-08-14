# Losing production API access, 2026-08-14

## What happened

At approximately 02:16 UTC on 2026-08-14 the venue's production API stopped
completing TLS handshakes from this address. TCP connects to port 443 succeed;
the peer resets during the handshake. DNS resolves normally, general internet
access is unaffected, and the venue's **demo** host continues to serve requests
normally. That combination is an address-level refusal at the edge, not an
outage and not a local network fault.

Collection stopped at 02:16. The collector kept retrying until it was stopped
at 17:54, logging `2 cycles over 15h38m`. Fifteen and a half hours of
collection were lost, and with them the Milestone 1 exit gate in its current
form.

The archive itself is intact: 39,645 snapshots spanning 2026-08-13 14:14 to
2026-08-14 02:16.

## Cause

Four consumers were pointed at the venue at the same time:

| consumer | rate |
| --- | --- |
| collector (120 markets / 30s) | ~4/s |
| fast-poll probe (6 legs / 1s) | 6/s |
| relationship proposal sweep | 6/s |
| venue-wide survey | 5/s |

Each had its own rate limiter. Each was comfortably inside the ceiling that
limiter knew about. The venue counts per address, and the sum was not.

The rate limiter's own docstring states the reasoning that failed:

> The limiter here is deliberately set below that. A 429 during a seven-day
> collection run leaves a hole in the archive, and a hole is not recoverable
> after the fact.

That is correct and it is per-process. **A rate limit enforced per process is
not enforced.** Every component was right in isolation; the sum was the thing
nobody owned.

A second failure compounded it. The client's retry ladder is six attempts
backing off to 63 seconds, which is right for a collector -- a dropped poll
leaves an unrecoverable hole. Against a refusal it is exactly wrong: the
collector spent fifteen hours patiently re-knocking on a door that had been
closed, which produced no data and gave the venue fifteen hours of unwanted
traffic.

## What was changed

**A shared venue budget** (`arbbot.venues.budget`, migration 0005). Every
consumer leases a share of one ceiling before its first request. If the live
total would exceed it, the consumer refuses to start and the error names who
holds the budget. Leases live in the database because it is the only thing the
containerised collector and the host-side probe share, and they heartbeat so a
crashed consumer's share returns to the pool rather than locking the venue out
until someone notices.

A consumer may not shrink its own rate to fit. Choosing a smaller number
unilaterally is precisely the reasoning that caused this.

**A circuit breaker** (`arbbot.venues.kalshi.rest`). Three consecutive
*transport* failures and the client stops and raises `VenueUnreachable`,
distinct from an ordinary error so a caller can tell "one request failed" from
"we appear to be blocked". HTTP errors deliberately do not trip it: a 404 is
the venue answering, and conflating the two would take a working collector
offline over one delisted market. A success resets the count, so a flaky
network is not mistaken for a block.

The collective ceiling is 10/s against a venue ceiling near 20/s. The margin is
not politeness -- the block happened while every component believed it was well
inside the limit, and being wrong costs days of collection rather than
milliseconds of latency.

## What was not done

No attempt was made to evade the block. No proxying, no address rotation, no
altering the client's identifying headers. A venue's access controls are the
venue's decision, and circumventing them would breach its terms and risk
turning a temporary refusal into a permanent one.

## Standing rules

1. Do not poll production to check whether access has returned. One deliberate
   check, spaced widely, at most.
2. Run the venue survey **before** restarting collection when access returns:
   it is a single cheap sweep and it decides whether there is any reason to
   collect at all.
3. Credentialed access is the supported path for this request volume. The block
   is the venue's answer to doing it unauthenticated.
