"""Deterministic controls between a qualified candidate and an order (FR-011).

The risk limits have existed in configuration since Milestone 0. ``doctor``
prints them. Nothing has ever enforced them, because until now there was no
execution path to enforce them against -- and a limit that is only displayed is
a limit that will be discovered to be missing at the worst possible moment.

Everything here is deterministic and refuses by default. A control that cannot
evaluate returns a refusal, never a pass: the whole point of a risk gate is
that the failure mode is "did not trade" rather than "traded without checking".

Four properties are worth stating, because each is a way this could be quietly
useless.

**The gate runs on state, not on intentions.** Exposure is summed from the
intents that are actually open, so an intent stuck in ``UNKNOWN`` counts
against the limit exactly like a filled one. An uncertain venue response is a
position until reconciliation proves otherwise, and a gate that ignored it
would size the next basket as though the money were free.

**A halting state stops everything.** ``UNKNOWN`` and ``INCIDENT`` mean the
system does not know what it holds. Placing a new order in that condition is
how one unreconciled position becomes several.

**The daily loss limit is measured from realised *and* unwind losses.** A
strategy that is profitable except for the cost of getting out of failed
baskets is not profitable, and a loss limit that only counted realised P&L
would let unwinds run unbounded.

**Limits are compared against the total after this basket, not before it.**
Checking headroom before adding the candidate is the classic off-by-one that
lets the last order breach the cap it was supposed to respect.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from arbbot.config import RiskLimits
from arbbot.money import ZERO, to_usd
from arbbot.reasons import RejectionReason
from arbbot.states import OrderState, halts_strategy, is_exposed

__all__ = ["ExposureSnapshot", "OpenIntent", "RiskDecision", "RiskGate"]


@dataclass(frozen=True, slots=True)
class OpenIntent:
    """One order intent the system currently believes is live."""

    intent_id: str
    state: OrderState
    committed: Decimal
    """Capital at risk: what has been paid, or would be owed if every resting
    leg filled. Never netted against expected payout -- a payout that has not
    arrived is not capital."""

    unmatched: Decimal = ZERO
    """The part of the basket that is acquired but not yet hedged. This is the
    directional exposure a multi-leg strategy actually carries."""


@dataclass(frozen=True, slots=True)
class ExposureSnapshot:
    """What the system currently has at risk, and what it has lost today."""

    intents: Sequence[OpenIntent] = ()
    realised_loss_today: Decimal = ZERO
    """Positive means money lost. Includes unwind losses and fees, because a
    strategy profitable except for its costs is not profitable."""

    @property
    def open_intents(self) -> list[OpenIntent]:
        return [i for i in self.intents if is_exposed(i.state)]

    @property
    def total_committed(self) -> Decimal:
        return sum((i.committed for i in self.open_intents), ZERO)

    @property
    def total_unmatched(self) -> Decimal:
        return sum((i.unmatched for i in self.open_intents), ZERO)

    @property
    def halting(self) -> list[OpenIntent]:
        """Intents whose state means the system does not know what it holds."""
        return [i for i in self.intents if halts_strategy(i.state)]


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Whether a candidate may proceed, and if not, which control refused it."""

    allowed: bool
    reason: RejectionReason | None = None
    detail: str = ""

    @classmethod
    def allow(cls) -> RiskDecision:
        return cls(allowed=True)

    @classmethod
    def refuse(
        cls, detail: str, reason: RejectionReason = RejectionReason.RISK_LIMIT
    ) -> RiskDecision:
        return cls(allowed=False, reason=reason, detail=detail)


class RiskGate:
    """Applies every deterministic control to a candidate basket."""

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def evaluate(
        self,
        *,
        notional: Decimal,
        net_edge: Decimal,
        quote_age: dt.timedelta | None,
        exposure: ExposureSnapshot,
        now: dt.datetime | None = None,
    ) -> RiskDecision:
        """Decide whether this basket may be submitted.

        :param notional: capital this basket would commit, fees included.
        :param net_edge: what the detector says survives every cost.
        :param quote_age: age of the oldest quote priced. ``None`` is refused
            rather than assumed fresh -- an evaluation that cannot say how old
            its inputs were has not measured anything.
        """
        del now  # every control here is state-based; kept for signature stability
        limits = self._limits

        halting = exposure.halting
        if halting:
            # The system does not know what it holds. Adding a position now is
            # how one unreconciled intent becomes several.
            names = ", ".join(f"{i.intent_id}({i.state.value})" for i in halting[:3])
            return RiskDecision.refuse(
                f"{len(halting)} intent(s) in a halting state: {names}. "
                f"Execution is blocked until a human resolves them.",
                RejectionReason.ORDER_STATE_UNKNOWN,
            )

        if quote_age is None:
            return RiskDecision.refuse(
                "quote age unknown; an evaluation that cannot say how old its inputs "
                "were has not measured anything",
                RejectionReason.STALE_QUOTE,
            )
        age_ms = int(quote_age.total_seconds() * 1000)
        if age_ms > limits.max_quote_age_ms:
            return RiskDecision.refuse(
                f"quote age {age_ms}ms exceeds {limits.max_quote_age_ms}ms",
                RejectionReason.STALE_QUOTE,
            )

        if to_usd(net_edge) < limits.min_net_edge_usd:
            return RiskDecision.refuse(
                f"net edge ${net_edge} is below the ${limits.min_net_edge_usd} floor",
                RejectionReason.NONPOSITIVE_NET_EDGE,
            )

        notional = to_usd(notional)
        if notional <= ZERO:
            return RiskDecision.refuse("a basket committing no capital is not an order")
        if notional > limits.max_order_notional_usd:
            return RiskDecision.refuse(
                f"order notional ${notional} exceeds the ${limits.max_order_notional_usd} cap"
            )

        # Compared *after* adding this basket. Checking headroom beforehand is
        # the off-by-one that lets the last order breach the cap it respects.
        total_after = exposure.total_committed + notional
        if total_after > limits.max_total_open_exposure_usd:
            return RiskDecision.refuse(
                f"total open exposure would reach ${total_after}, over the "
                f"${limits.max_total_open_exposure_usd} cap "
                f"({len(exposure.open_intents)} intent(s) already open)"
            )

        # The whole basket is unmatched between the first leg and the last, so
        # that is what this control has to be sized against -- not the residual
        # left over afterwards.
        unmatched_after = exposure.total_unmatched + notional
        if unmatched_after > limits.max_unmatched_exposure_usd:
            return RiskDecision.refuse(
                f"unmatched exposure would reach ${unmatched_after} while acquiring, "
                f"over the ${limits.max_unmatched_exposure_usd} cap"
            )

        if exposure.realised_loss_today >= limits.daily_loss_limit_usd:
            return RiskDecision.refuse(
                f"daily loss ${exposure.realised_loss_today} has reached the "
                f"${limits.daily_loss_limit_usd} limit; no further orders today"
            )

        return RiskDecision.allow()
