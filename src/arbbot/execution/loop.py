"""The loop from a detected opportunity to a basket waiting for a person.

This is the last structural piece, and the shape of it is the point: **the loop
does not trade.** It checks the halt, prices what detection found, applies every
deterministic control, and parks whatever survives in ``AWAITING_HUMAN``. A
person approves a specific basket, and only then does the executor run.

That is FR-016 taken literally rather than decoratively. A loop that could arm
itself would make the third gate a formality -- the approval would exist, but
nothing would ever be waiting on it. So the loop's terminal state on the happy
path is "waiting", and the thing that makes an order happen is a separate,
deliberate act.

Three properties beyond that.

**The halt is checked first, before anything is priced.** Pricing candidates
that cannot be traded costs nothing and produces a record of opportunities that
were never real, which is worse than useless -- it is a table someone will
later count.

**Approvals expire, and quickly.** A basket approved ten minutes after it was
priced is a basket priced on quotes that are long gone; the freshness gate
would reject it at submission, but by then a person has already said yes to
something that no longer exists. Expiring first means they are only ever asked
about live prices.

**Nothing self-approves, including on retry.** An expired intent is terminal.
The loop proposes a new one at current prices rather than reviving the old
approval, because an approval is bound to what was shown at the time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from arbbot.collection.health import as_utc
from arbbot.execution.executor import BasketIntent
from arbbot.execution.store import ExecutionStore
from arbbot.money import ZERO
from arbbot.reasons import RejectionReason
from arbbot.risk import RiskGate, TradingHalt
from arbbot.states import OrderState

__all__ = ["APPROVAL_TTL", "Candidate", "LoopReport", "TradingLoop"]

#: How long a basket may wait for a human before it is no longer the basket
#: that was priced.
#:
#: Deliberately short. The detector's freshness gate is two seconds; this is
#: the window in which a person can look at a price and have it still be true,
#: and stretching it does not buy patience, it buys stale approvals.
APPROVAL_TTL: Final = dt.timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A detected opportunity, before any control has been applied."""

    intent_id: str
    relationship_slug: str
    relationship_approved: bool
    legs: tuple[tuple[str, Decimal], ...]
    quantity: Decimal
    net_edge: Decimal
    quote_age: dt.timedelta | None

    def to_intent(self, *, human_approved: bool = False) -> BasketIntent:
        return BasketIntent(
            intent_id=self.intent_id,
            legs=self.legs,
            quantity=self.quantity,
            net_edge=self.net_edge,
            quote_age=self.quote_age,
            relationship_approved=self.relationship_approved,
            human_approved=human_approved,
        )


@dataclass(slots=True)
class LoopReport:
    """What one pass decided."""

    halted: bool = False
    halt_detail: str = ""
    considered: int = 0
    awaiting: int = 0
    rejected: int = 0
    expired: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def render(self) -> str:
        if self.halted:
            return f"loop: halted, nothing priced\n  {self.halt_detail}"
        lines = [
            f"candidates considered : {self.considered}",
            f"awaiting approval     : {self.awaiting}",
            f"rejected              : {self.rejected}",
            f"expired               : {self.expired}",
        ]
        for reason, count in sorted(self.reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {reason:<28} {count:>5}")
        if self.awaiting:
            lines.append("")
            lines.append("Nothing has been ordered. Each basket above is waiting on a person,")
            lines.append("and expires in well under a minute -- an approval given after that")
            lines.append("would be an approval of prices that no longer exist.")
        return "\n".join(lines)


class TradingLoop:
    """Detection to a basket awaiting approval. Never to an order."""

    def __init__(
        self,
        store: ExecutionStore,
        risk: RiskGate,
        halt: TradingHalt,
        *,
        approval_ttl: dt.timedelta = APPROVAL_TTL,
    ) -> None:
        self._store = store
        self._risk = risk
        self._halt = halt
        self._ttl = approval_ttl

    def expire_stale(self, *, now: dt.datetime) -> int:
        """Retire approvals nobody answered in time.

        Run before proposing anything, so a person is never shown a queue in
        which some entries are already meaningless.
        """
        expired = 0
        for row in self._store.awaiting_human():
            if now - as_utc(row.updated_ts) > self._ttl:
                self._store.transition(
                    row.intent_id,
                    OrderState.EXPIRED,
                    reason=str(RejectionReason.APPROVAL_EXPIRED),
                    detail=(
                        f"waited longer than {self._ttl.total_seconds():.0f}s; the prices "
                        f"it was priced on are gone"
                    ),
                    now=now,
                )
                expired += 1
        return expired

    def cycle(
        self,
        candidates: list[Candidate],
        *,
        now: dt.datetime,
        realised_loss_today: Decimal = ZERO,
    ) -> LoopReport:
        """Apply every control to what detection found, and park the survivors."""
        report = LoopReport()

        # Before anything is priced. Pricing candidates that cannot be traded
        # produces a record of opportunities that were never real, which is a
        # table somebody will later count.
        state = self._halt.state
        if not state.may_trade:
            report.halted = True
            report.halt_detail = state.render()
            return report

        report.expired = self.expire_stale(now=now)

        for candidate in candidates:
            report.considered += 1
            intent = candidate.to_intent()

            if not candidate.relationship_approved:
                report.rejected += 1
                report.note(str(RejectionReason.RELATIONSHIP_NOT_APPROVED))
                continue

            decision = self._risk.evaluate(
                notional=intent.notional,
                net_edge=intent.net_edge,
                quote_age=intent.quote_age,
                # Parked baskets count here. They hold nothing yet, but a queue
                # of sixty that each fit individually and cannot all be taken is a
                # queue that lies to whoever reads it. The executor re-checks
                # without them, against what is genuinely at risk.
                exposure=self._store.exposure(
                    realised_loss_today=realised_loss_today, include_pending=True
                ),
            )

            row = self._store.open_intent(
                intent, relationship_slug=candidate.relationship_slug, now=now
            )
            if not decision.allowed:
                self._store.transition(
                    row.intent_id,
                    OrderState.RISK_REJECTED,
                    reason=str(decision.reason) if decision.reason else None,
                    detail=decision.detail,
                    now=now,
                )
                report.rejected += 1
                report.note(str(decision.reason) if decision.reason else "risk_limit")
                continue

            self._store.transition(row.intent_id, OrderState.RISK_APPROVED, now=now)
            # Parked, not submitted. The loop's happy path ends at "waiting"
            # because a loop that could arm itself would make the human gate a
            # formality -- present in the design, never actually waited on.
            self._store.transition(
                row.intent_id,
                OrderState.AWAITING_HUMAN,
                detail="every deterministic control passed; waiting on a person",
                now=now,
            )
            report.awaiting += 1

        return report
