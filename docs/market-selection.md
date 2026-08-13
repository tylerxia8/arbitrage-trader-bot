# Market selection

**Surveyed 2026-08-13** against the live production API, ahead of the
Milestone 1 seven-day collection run. Answers the Appendix D question "which
first market category to map", and produces an early reading on the
specification's first hypothesis.

**Recommendation: daily temperature partitions.** This confirms the
specification's own guess, but by measurement rather than assumption — and the
route there found a trap worth more than the conclusion.

---

## The trap: `mutually_exclusive` is not enough

Kalshi flags events `mutually_exclusive`. That is exactly the attribute an
exhaustive-basket detector appears to want, and using it as the filter is
actively dangerous.

Filtering 1,200 live events on that flag and pricing the largest 40 groups
produced four baskets costing **less than a dollar**:

| Event | Legs | Basket cost | Apparent gap |
|---|---|---|---|
| KXPRESMATCHUP-28NOV07 | 16 | $0.7460 | +$0.2540 |
| KXDTICKET-28NOV07 | 25 | $0.8400 | +$0.1600 |
| KXVPRESNOMR-28 | 22 | $0.8710 | +$0.1290 |
| KXRTICKET-28NOV07 | 25 | $0.9070 | +$0.0930 |

A 25% edge on a guaranteed dollar, on a regulated venue, sitting in public
order books. That is not what an arbitrage looks like; it is what a wrong
assumption looks like.

Every one of them enumerates named candidates out of an unbounded space —
sixteen specific 2028 presidential matchups, out of every pair that could
occur. The flag asserts that **at most** one outcome wins. A basket needs
**exactly** one: at most one *and* at least one. The missing $0.254 is the
market correctly pricing the chance that none of the listed matchups happens.
Buy all sixteen legs and an unlisted matchup pays nothing — a total loss on
the basket, not a 25% gain.

This is the failure mode FR-005 exists to prevent, and it is worth noting how
it presents: not as an error, but as the single most attractive number on the
screen.

## The discriminator: `strike_type`

| Type | Meaning | Exhaustive? |
|---|---|---|
| `between`, `less`, `greater` | numeric buckets tiling a real line | **yes**, by construction |
| `custom` | named candidates | no — covers only what was listed |
| `structured`, unset | unclassified | unknown; needs terms review |

A numeric partition is exhaustive because the underlying is a *number*: every
value falls in exactly one bucket, provided both tails exist. `KXGDPYEAR-36`
is the clean example — a `less` bucket, twelve `between` buckets tiling
0.1→6.0, and a `greater` bucket.

Implemented in `arbbot.venues.kalshi.discovery`. It **proposes**; it never
approves. Only `PARTITION` may be drafted, and a draft still enters the
registry as `PENDING`.

## What the structurally valid sets actually cost

Pricing every genuine partition found (executable YES asks, best level, **no
fees**):

| Universe | Count | Cheapest | Median | Below par |
|---|---|---|---|---|
| Long-dated (GDP 2027–2036, senate seats) | 13 | $1.0100 | — | **0** |
| Daily temperature | 10 | $1.0300 | $1.0900 | **0** |
| Enumerated (contrast) | 8 | $1.0920 | — | 0 |

Not one structurally valid basket priced below a dollar. On the liquid daily
partitions the guaranteed dollar costs **3 to 13 cents more than a dollar**,
before any fee.

### How much this is and is not evidence

It is one snapshot. The hypothesis in §8 is that inconsistencies *occur* —
transiently — and a single observation cannot refute that. It is exactly what
the seven-day collection exists to test.

What it does establish is the size of the gap that would have to close. For a
candidate to qualify, a 3-cent spread must not merely vanish but invert past
the fee. That is a useful prior to hold before reading the M3 falsification
report, and a useful check on any future result that looks generous.

## Recommended collection universe

**Daily temperature partitions** (`KXHIGH*`, `KXLOW*`, `KXTEMP*`).

- **Genuinely exhaustive.** Numeric buckets with both tails.
- **Liquid.** Per-leg volumes in the hundreds to low thousands, against
  handfuls on the long-dated partitions.
- **They resolve daily.** Many independent observations per week rather than
  one long-dated set observed repeatedly — which matters for a hypothesis
  about how *often* something occurs.
- **Small and stable leg counts.** Six legs, so a basket is acquirable and a
  cycle stays inside its poll interval.

103 temperature series exist; roughly 10 events are live at any moment
(≈60 markets), which fits comfortably inside the rate limit at a 15-second
cadence.

**Not recommended for collection:** anything `custom`. Not because it is
uninteresting, but because a detector pointed at it will keep finding the
$0.746 basket and being wrong in the most expensive available direction.

## Open questions for the owner

1. ~~**Boundary semantics.**~~ **Resolved by reading the rules.** The
   `floor_strike`/`cap_strike` fields looked like they overlapped — `91° or
   below` carries `cap=92` while the next bucket has `floor=92`. The
   settlement text says why:

   | Bucket | Rule text | Covers |
   |---|---|---|
   | `91° or below` | "is **less than** 92°" | (−∞, 92) |
   | `92° to 93°` | "is **between** 92-93°" | [92, 93] |
   | `100° or above` | "is **greater than** 99°" | (99, +∞) |

   So `less` is strictly-below-cap, `between` is inclusive both ends, and
   `greater` is strictly-above-floor. Under that convention the set has real
   holes on the reals — **93.5 resolves nothing** — and tiles perfectly on the
   integers. Checked across every live temperature partition: **10 of 10 tile
   cleanly**, so the convention is consistent rather than incidental.
   Implemented as `check_integer_coverage` (FR-008).

   **The residual assumption is the important part.** Exhaustiveness depends
   entirely on the settlement source reporting whole degrees. The rules name
   the NWS Climatological Report (Daily). If that source ever published a
   fractional reading, a basket holding every leg would pay **zero** on a
   value that fell in a hole. That belongs in the approval record as a stated
   dependency, not as an unexamined background fact.

2. **Settlement source — still open, and it has teeth.** The rules say to
   "use the **latest version** of the data for the desired date, keeping in
   mind that different cities update their reports at different frequencies."
   NWS reports are revised. A revision after settlement could move which
   bucket won. Worth understanding before capital is committed, and worth
   watching via the terms-hash monitoring in FR-004.

3. **Fees are still unmodelled.** Every number here is gross. The fee service
   is Milestone 2 (EPIC-9), and until it exists no figure in this document
   should be read as an edge.
