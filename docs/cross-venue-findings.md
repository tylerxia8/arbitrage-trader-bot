# Cross-venue: Kalshi against Polymarket, 2026-08-19

A first probe, after both routes inside Kalshi measured negative. One snapshot,
no relationship approved, nothing traded.

## The trade

Kalshi market K and Polymarket market P claim the same thing. Buy YES on K at
its ask, buy NO on P at its ask (which for complementary binary tokens is
`1 - bestBid(Yes)`). Exactly one pays a dollar whichever way the world goes, so
the position is worth $1.00 and costs the sum. Edge is `1.00 - (K_yes + P_no)`.

Structurally this is the two-leg basket the detector already prices. What is
new is that the legs sit on different venues, and the claim that they are the
same claim is now the entire risk.

## Matched markets exist, with real depth

| candidate (2028 US Presidential Election winner) | K ask | P bid | K_yes + P_no | PM liquidity |
| --- | --- | --- | --- | --- |
| Alexandria Ocasio-Cortez | 0.0960 | 0.1290 | **0.9670** | $394k |
| Greg Abbott | 0.0040 | 0.0080 | 0.9960 | $2.25M |
| Nikki Haley | 0.0020 | 0.0050 | 0.9970 | $2.25M |
| Glenn Youngkin | 0.0040 | 0.0060 | 0.9980 | $2.17M |
| Vivek Ramaswamy | 0.0020 | 0.0020 | 1.0000 | $1.61M |
| Gavin Newsom | 0.0940 | 0.0830 | 1.0110 | $394k |
| Andy Beshear | 0.0320 | 0.0100 | 1.0220 | $371k |

Polymarket spreads are a tenth of a cent on the liquid names, against 42-51
cents across a thin Kalshi partition. This is not the same kind of market.

## Three findings, in order of importance

**1. Naive title matching is dangerous, and produced a false positive
immediately.** Kalshi lists "Who will **run for** the 2028 Republican
presidential nomination?"; Polymarket lists "Will X **win** the 2028 Republican
presidential nomination?". A token-similarity matcher pairs them at 0.86. They
are different claims, running is far likelier than winning, and the resulting
price gap would read as an enormous arbitrage while actually being a large
directional bet. **No pair may be priced without a human reading both
settlement texts** -- the registry gate exists for exactly this and now matters
more than it ever did inside one venue.

**2. Gross edge exists.** 3.3 cents on the AOC pair, and a few tenths of a cent
on three others. That is more than anything measured inside Kalshi, where the
best confirmed-freshness basket was negative after fees.

**3. And the calendar eats it.** These settle in **2028**. Three and a bit cents
on 97 cents held for roughly two and a half years is well under one percent a
year, before Kalshi's fee. That fee is `ceil(0.07 x C x P x (1-P))` with a
one-cent floor per contract, so a 9.6-cent contract pays a full cent -- about a
third of the gross edge -- and the position still has to survive to 2028.

The ledger has tracked capital-days since M3 precisely because a return on
capital that sat idle is not comparable to anything. This is that number
mattering.

## What would actually be worth measuring

Short-dated matched pairs. The same three cents on a market resolving in a week
is a completely different annualised figure, and nothing here says whether such
pairs exist -- the sample was ordered by volume, which selects for long-dated
political markets.

## What is not established

* That any pair above settles identically. Nobody has read the terms.
* Polymarket's fee schedule. The API reports `takerBaseFee: 1000` in units this
  project has not confirmed, and an unverified fee may not qualify anything --
  the same rule Kalshi's schedule was held to.
* Depth at the quoted prices, or whether either side fills.
* Anything about a snapshot's persistence. One moment, one read.
