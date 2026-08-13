# Kalshi venue findings

**Verified 2026-08-12** against `docs.kalshi.com` and the live production API.
Re-verify before any live or commercial release; the specification's own source
register warns that venue documentation and fee schedules change.

Three findings contradicted assumptions the build was carrying. Two are already
fixed in code; one is an open decision for the owner.

---

## 1. Prices and sizes are not integers (fixed)

Milestone 1 modelled the book as integer cents and whole contracts. Both are
wrong under the venue's fixed-point format.

| | Assumed | Actual |
|---|---|---|
| Price | `int` cents, 1–99 | decimal **string**, up to 4 dp, e.g. `"0.5900"` |
| Quantity | `int` contracts | fixed-point **string**, 2 dp, e.g. `"809.25"` |
| Tick size | always $0.01 | per-market: `linear_cent` $0.01, `deci_cent` $0.001 |

A live capture of `KXNFLGAME-26AUG15DALSEA-SEA` shows resting sizes of
`809.25`, `2654.72`, `10214.24` — fractional positions are ordinary, not an
edge case. Minimum granularity is 0.01 contracts.

Both fields are now `Decimal` throughout `arbbot.marketdata`, parsed by
`parse_venue_dollars` and `parse_quantity`, which reject floats and reject
precision finer than the venue documents (a signal the wire format changed).

The values arrive as strings precisely so they survive transport exactly.
Letting a JSON library turn them into floats — the default behaviour — throws
away the precision the venue went to the trouble of preserving. That would be
FR-002's exact failure mode, arriving through the front door.

## 2. The orderbook is public; the docs disagree with themselves (resolved)

The API reference page for `get-market-orderbook` lists
`kalshiAccessKey`/`kalshiAccessSignature` as required. The market-data
quick-start says the same endpoint is public.

Resolved empirically: an unauthenticated `GET /markets/{ticker}/orderbook`
against production **succeeds**. The API reference is listing a global security
scheme, not an endpoint-specific requirement. `GET /markets` is public too.

Milestone 1 can therefore collect order books with no credential, as the
specification assumed.

Verified base URL: `https://external-api.kalshi.com/trade-api/v2`
(not `api.elections.kalshi.com`, which the M0 config defaulted to).

## 3. The WebSocket requires a credential — open decision

> "Authentication is required to establish the connection; include API key
> headers during the WebSocket handshake. Some channels carry public data, but
> the connection itself still requires authentication."

This conflicts with the plan in a way engineering cannot resolve alone:

- §5 records "public market and order-book access is available" — true for
  REST, **not** for streaming.
- The delivery plan says do not authorize credentials during M0–M3.
- `arbbot.config` actively **rejects** a credential in the `research`
  environment, by design (threat T1).

So `orderbook_delta` streaming is unavailable credential-free. The options:

**(a) Poll REST.** Works today, no credential, no policy change. Costs time
resolution: polling sees the book every N seconds, and an opportunity shorter
than the poll interval is invisible. Since M3 measures *opportunity duration
against execution latency*, a coarse sampler may understate how much edge
exists — biasing the falsification result toward "no edge", which is the safe
direction to be wrong in, but still wrong.

**(b) Issue a read-only key for research.** Full delta stream, real time
resolution. Requires relaxing the config rule to permit a market-data-only
credential in `research`, and accepting a key on a machine running unattended
for seven days. The key would carry no trading permission.

**(c) Defer streaming to M4**, when demo credentials exist anyway, and run M1–M3
on polled REST.

**Recommendation: (a) now, revisit at M3.** Polling is enough to prove the
collection, replay, and detection machinery, and the duration question can be
re-opened once there is evidence that any candidates exist at all. Adopting (b)
before knowing whether the strategy has any signal spends a policy exception on
a question that may not matter.

Only the REST client is implemented. No WebSocket client was written, because
writing one that cannot be run or tested would be speculative code sitting in
the money path's blast radius.

---

## Reference

| Item | Value |
|---|---|
| REST base (production) | `https://external-api.kalshi.com/trade-api/v2` |
| REST base (demo) | `https://external-api.demo.kalshi.co/trade-api/v2` |
| WebSocket (production) | `wss://external-api-ws.kalshi.com/` |
| WebSocket (demo) | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` |
| Rate limit | token bucket; most calls cost 10 tokens; Basic read tier 200 tokens/s ≈ 20 req/s |
| Rate limit exceeded | `429` with `{"error": "too many requests"}`, **no** `Retry-After`; exponential backoff |
| Orderbook shape | `{"orderbook_fp": {"yes_dollars": [[price, count]], "no_dollars": [...]}}` |
| Sort order | ascending; **last** element is the best (highest) bid |
| Sides | resting **bids** on both outcomes — never asks |
| Market status | `"active"` is the open state (not `"open"`) |
| Demo credentials | separate from production; demo prices "may not be reflective of real markets" |

### Both sides are bids

The single most consequential detail. The book quotes resting bids on YES and
on NO; asks are implied:

```
best YES ask = $1.00 − (best NO bid)
best NO  ask = $1.00 − (best YES bid)
```

Confirmed against the venue's own published quotes: with a best NO bid of
`0.4000`, the venue reported `yes_ask_dollars` of `0.6000`. The book derives
asks rather than storing them, and `test_matches_the_venues_own_quoted_ask`
pins this to the captured fixture.

Reading a YES bid as a YES ask would make every basket look roughly half its
true cost — a bug that presents as a spectacular arbitrage rather than as an
error, which is the worst way for it to present.
