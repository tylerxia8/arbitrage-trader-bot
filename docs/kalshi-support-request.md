# Draft support request to Kalshi

Send from your own account, from the email address the account is registered
to. Edit freely — the only parts worth keeping verbatim are the admission of
what we did and the specific numbers, because those are what let a support
engineer find the block and decide it is safe to lift.

---

**Subject:** Public API access blocked from my address — self-inflicted rate
overrun, remediated

Hello,

I appear to have been blocked from `external-api.kalshi.com` and I believe it
was my own fault. I would like to explain what happened, what I have changed,
and ask what the appropriate path forward is.

**What I was doing.** I am building a personal research project that studies
whether logical arbitrage exists in multi-outcome markets — specifically
whether the legs of a mutually exclusive, collectively exhaustive event ever
price below their guaranteed one-dollar payout. It reads public market data
only: `GET /series`, `GET /events`, and `GET /markets/{ticker}/orderbook`. It
has never placed an order, holds no credentials, and has no order-placement
code in it at all.

**What went wrong.** On 2026-08-14 I had four separate processes reading at
once — a 30-second collector across 120 markets, a 1-second probe on 6 markets,
a market-structure sweep, and a venue-wide pricing survey. Each had its own
rate limiter set below what I understood the ceiling to be. I had not accounted
for the fact that your limit is per address and mine were per process, so the
combined rate was roughly 20 requests/second sustained rather than the 4–6 I
believed. At about 02:16 UTC the host stopped completing TLS handshakes from my
address.

**What I have changed.** All my processes now lease a share of a single shared
request budget before making any request, and a process that would push the
total over the ceiling refuses to start rather than running anyway. I have
capped the collective ceiling at 10 requests/second. I have also added a
circuit breaker that stops after three consecutive transport failures, so I
cannot repeat what I did after the block — my collector kept retrying for
fifteen hours before I noticed, which I realise added unwanted load at exactly
the wrong moment. I am sorry about that.

**What I am asking.**

1. Could the block on my address be reviewed and lifted?
2. What sustained request rate is acceptable for unauthenticated public market
   data? I would rather work to a number you give me than to one I inferred.
3. Would an authenticated API key be the more appropriate way to do this
   volume of reads? If so, I am happy to move to one, and would appreciate
   knowing the rate limits that apply.

I have deliberately not attempted to work around the block in any way, and I
have stopped all traffic to production while waiting for your reply.

Thank you,
[your name]
[account email]
[approximate source IP, if you are comfortable including it]

---

## Notes before sending

- Include the timestamp (**2026-08-14 ~02:16 UTC**) and your source IP if you
  know it. Both make the block findable in their logs, and a support engineer
  who can find it is far more likely to lift it.
- Do not send this more than once, and do not follow up quickly. A second
  message adds nothing and reads as pressure.
- If they grant credentialed access, that decision belongs in the repo:
  `arbbot.venues.kalshi.auth` exists but is marked unverified and refuses to
  sign until a human confirms the scheme against the venue's current docs — the
  same rule the fee schedule follows.
