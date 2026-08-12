# ADR-0003: Fixed-point money, enforced statically

**Status:** Accepted · 2026-08-12

## Context

FR-002 requires Decimal or fixed-point arithmetic for every price, quantity,
fee, and P&L figure, and requires a static check or test that rejects floats in
money modules.

The requirement is not stylistic. This system exists to decide whether a
guaranteed payout can be acquired for less than it pays out, and the margins it
is looking for are cents on a dollar. A binary float cannot represent `0.35`
exactly; `Decimal(0.35)` is `0.34999999999999997779553950749686919152736663818359375`.
An error of that size is small compared to a trade and large compared to the
edge being measured, which is the worst possible ratio: it will not show up in
testing and it will corrupt exactly the decision that matters.

Rounding direction is the same problem in a different form. If costs round down
or payouts round up, a basket with no real edge can round into apparent profit.
The system would then trade on rounding residue, and the resulting losses would
look like ordinary execution slippage rather than a bug.

Neither ruff nor mypy can express "no float in these particular modules" —
`float` is an ordinary type, and the prohibition is scoped to a subset of the
codebase.

## Decision

1. All money is `decimal.Decimal`, constructed only from exact sources (`int`,
   `str`, `Decimal`). `arbbot.money.to_usd` rejects `float` at runtime rather
   than converting it.

2. Rounding always favours rejection: `quantize_cost` rounds up,
   `quantize_proceeds` rounds down. A computed net edge is therefore a lower
   bound on the true edge, never an upper one.

3. Money columns are `NUMERIC(18, 8)`. `DOUBLE PRECISION` is prohibited, and a
   test asserts no float column exists in the metadata — the Python-side check
   cannot see a database that rounds on the way in.

4. `tools/check_no_float.py` enforces the rule at the AST level over the roots
   declared in `[tool.arbbot] money_path_roots`, flagging float literals, uses
   of the name `float`, and imports of float-returning modules (`math`,
   `statistics`, `numpy`, `random`, `cmath`). It runs in CI and in the test
   suite.

`isinstance(x, float)` is exempt: it is a rejection of float, which is the
behaviour the rule exists to produce. A reviewed exception may be marked
`# money-path: allow`, which deliberately avoids ruff's `# noqa:` namespace.

Roots that do not exist yet (`fees`, `economics`, `detector`, `ledger`,
`execution`) are declared now so the rule applies to the first commit that
creates them, rather than being remembered later.

## Consequences

- Venue integers convert at the boundary via `from_cents`; nothing downstream
  handles a raw venue integer as if it were dollars.
- Float-based numerical libraries cannot be used in the money path. Where one is
  genuinely needed for analytics, it belongs outside those roots, and its output
  cannot feed a qualification decision.
- The checker is itself tested against planted violations. A static check that
  silently passes everything is worse than none, because it converts an
  unverified rule into a green tick.
