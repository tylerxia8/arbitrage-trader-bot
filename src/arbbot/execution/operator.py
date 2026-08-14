"""The operator's side of the human gate.

The loop parks baskets in ``AWAITING_HUMAN`` and the executor refuses anything
without ``human_approved``. Between those two facts there was nothing at all --
no way to see what was waiting, and no way to approve it. A gate with no
interface is not a gate that is hard to pass, it is a gate that has never been
tested, and the first time anyone tried to use this system they would have
found the design's central control unreachable.

What a reviewer is shown matters as much as that they are shown something.

**The queue shows what expires and when.** A basket priced thirty seconds ago
is not the basket in front of you now, so every row carries its age and the
list refuses to render one that has already lapsed without saying so.

**Approving names a person and what they checked**, exactly as the relationship
registry does. The reasons differ: there, the claim is that a leg set is
exhaustive; here, it is that this specific price, at this size, right now, is
worth taking. Both are claims a person is answerable for, and neither survives
being recorded as "approved by the system".

**Approval and execution are one action, deliberately.** Splitting them would
create a window in which a basket is approved and not yet sent, which is
another thing to expire and another state to be wrong about. The reviewer says
yes and the legs go, or nothing happens.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from arbbot.collection.health import as_utc, utc_now
from arbbot.db.models import OrderIntent
from arbbot.execution.executor import BasketIntent, ExecutionResult, Executor
from arbbot.execution.loop import APPROVAL_TTL
from arbbot.execution.store import ExecutionStore
from arbbot.money import ZERO
from arbbot.reasons import RejectionReason
from arbbot.states import OrderState

__all__ = ["ApprovalRefused", "OperatorConsole", "PendingBasket"]


class ApprovalRefused(RuntimeError):
    """The approval was not accepted, and no order was sent."""


@dataclass(frozen=True, slots=True)
class PendingBasket:
    """One basket waiting on a person, as they should see it."""

    intent_id: str
    relationship_slug: str
    quantity: Decimal
    notional: Decimal
    net_edge: Decimal
    waiting: dt.timedelta
    expires_in: dt.timedelta

    @property
    def lapsed(self) -> bool:
        return self.expires_in <= dt.timedelta(0)

    def render(self) -> str:
        status = "LAPSED" if self.lapsed else f"{self.expires_in.total_seconds():.0f}s left"
        return (
            f"  {self.intent_id:<20} {self.relationship_slug:<26} "
            f"qty {self.quantity:>6}  ${self.notional:>8}  edge ${self.net_edge:>7}  {status}"
        )


class OperatorConsole:
    """What a person uses to see and answer the queue."""

    def __init__(
        self,
        store: ExecutionStore,
        executor: Executor,
        *,
        approval_ttl: dt.timedelta = APPROVAL_TTL,
    ) -> None:
        self._store = store
        self._executor = executor
        self._ttl = approval_ttl

    def pending(self, *, now: dt.datetime | None = None) -> list[PendingBasket]:
        """Everything waiting, with how long it has left."""
        at = now or utc_now()
        baskets: list[PendingBasket] = []
        for row in self._store.awaiting_human():
            waited = at - as_utc(row.updated_ts)
            baskets.append(
                PendingBasket(
                    intent_id=row.intent_id,
                    relationship_slug=row.relationship_slug,
                    quantity=Decimal(row.quantity),
                    notional=Decimal(row.notional),
                    net_edge=Decimal(row.net_edge),
                    waiting=waited,
                    expires_in=self._ttl - waited,
                )
            )
        return baskets

    def render_queue(self, *, now: dt.datetime | None = None) -> str:
        baskets = self.pending(now=now)
        if not baskets:
            return "nothing is waiting on a person."

        lines = [f"{len(baskets)} basket(s) awaiting approval:", ""]
        lines.extend(b.render() for b in baskets)
        lines.append("")
        lines.append("Approving one sends its legs immediately. There is no separate step,")
        lines.append("because a gap between approving and sending is another window to")
        lines.append("expire in and another state to be wrong about.")
        if any(b.lapsed for b in baskets):
            lines.append("")
            lines.append("LAPSED rows were priced too long ago to approve. They will be")
            lines.append("retired on the next loop pass; a fresh one will be proposed if the")
            lines.append("opportunity is still there.")
        return "\n".join(lines)

    def reject(
        self, intent_id: str, *, reviewer: str, reason: str, now: dt.datetime | None = None
    ) -> OrderIntent:
        """Decline a basket. Terminal, and recorded against a name."""
        if not reviewer.strip():
            raise ApprovalRefused("a decision must name the person who made it")
        return self._store.transition(
            intent_id,
            OrderState.REJECTED,
            reason=str(RejectionReason.RISK_LIMIT),
            detail=f"declined by {reviewer}: {reason}",
            now=now,
        )

    async def approve(
        self,
        intent_id: str,
        *,
        reviewer: str,
        evidence: str,
        legs: tuple[tuple[str, Decimal], ...],
        quote_age: dt.timedelta | None,
        now: dt.datetime | None = None,
        realised_loss_today: Decimal = ZERO,
    ) -> ExecutionResult:
        """Approve one basket and acquire it.

        :raises ApprovalRefused: on anything that means the approval is not a
            considered decision about a live price -- an unnamed reviewer, no
            record of what was checked, an intent that is not waiting, or one
            that has been waiting too long.
        """
        at = now or utc_now()

        if not reviewer.strip():
            raise ApprovalRefused(
                "an approval must name a person; 'approved by the system' records nothing"
            )
        if not evidence.strip():
            raise ApprovalRefused(
                "an approval must say what was checked; the claim here is that this "
                "price, at this size, right now is worth taking, and that is something "
                "a person is answerable for"
            )

        row = self._store.find(intent_id)
        if row is None:
            raise ApprovalRefused(f"no intent {intent_id!r}")
        if OrderState(row.state) is not OrderState.AWAITING_HUMAN:
            raise ApprovalRefused(
                f"intent {intent_id!r} is {row.state}, not awaiting approval; only a "
                f"parked basket can be approved"
            )

        waited = at - as_utc(row.updated_ts)
        if waited > self._ttl:
            # Retired rather than approved. Saying yes to a price from two
            # minutes ago is saying yes to a price that is gone, and the
            # freshness gate would reject it a moment later anyway -- after a
            # person had already committed to it.
            self._store.transition(
                intent_id,
                OrderState.EXPIRED,
                reason=str(RejectionReason.APPROVAL_EXPIRED),
                detail=f"approval attempted after {waited.total_seconds():.0f}s",
                now=at,
            )
            raise ApprovalRefused(
                f"intent {intent_id!r} was priced {waited.total_seconds():.0f}s ago and has "
                f"expired; it has been retired and a fresh one will be proposed if the "
                f"opportunity is still there"
            )

        intent = BasketIntent(
            intent_id=intent_id,
            legs=legs,
            quantity=Decimal(row.quantity),
            net_edge=Decimal(row.net_edge),
            quote_age=quote_age,
            relationship_approved=True,
            human_approved=True,
        )

        # The approval is recorded on the row *before* the executor runs, so a
        # crash between the two leaves evidence that a person said yes. The
        # state itself is not moved here: the executor's gates run first, and
        # its journal owns every transition from this point. Moving to
        # SUBMITTING up front would strand the row there if a control refused,
        # since the machine has no way back.
        row.detail = f"approved by {reviewer}: {evidence}"

        # Exposure without pending baskets: what matters at submission is what
        # is genuinely at risk, not what else happens to be queued.
        return await self._executor.acquire(
            intent, exposure=self._store.exposure(realised_loss_today=realised_loss_today)
        )
