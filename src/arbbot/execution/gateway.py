"""What an execution venue must be able to do, and a paper one that does it.

The gateway is a protocol rather than a class so the executor can be built and
tested to completion without a credential, and so the real venue slots in
underneath without the executor learning anything about it. That is not
architectural neatness: the executor's hard parts are partial fills, unwinds
and uncertain responses, and those are far easier to test against a gateway
that can be *told* to lose contact than against a live venue that does it
occasionally.

Three requirements the protocol imposes on any implementation.

**Every submission carries an idempotency key, and the venue must honour it.**
A retried submit that fills twice turns one leg of a hedged basket into a
directional position -- the single most expensive bug available here. The key
is derived from the intent and the leg, so a retry of the same leg is
recognisably the same order and a genuinely new order cannot collide with it.

**An uncertain outcome is reported as uncertain.** ``OrderOutcome.UNKNOWN``
exists because a timeout is not a rejection. Code that treats "no response" as
"did not fill" and retries is how one leg becomes two, and the state machine
already refuses to let ``UNKNOWN`` go anywhere except reconciliation.

**Nothing here decides anything.** The gateway places what it is told to place.
Every gate -- approval, risk, the build flag, the human -- sits above it, so a
gateway that is wired up by mistake still cannot trade on its own.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from arbbot.money import ZERO

__all__ = [
    "OrderGateway",
    "OrderOutcome",
    "OrderRequest",
    "OrderResult",
    "PaperGateway",
]


class OrderOutcome(enum.StrEnum):
    """What the venue did with one leg order."""

    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    """No usable response. Treated as a possible fill, never as a no-op."""


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """One leg, priced and sized, with the key that makes retrying it safe."""

    idempotency_key: str
    ticker: str
    quantity: Decimal
    limit_price: Decimal
    """A limit, always. A market order in a book this thin is an invitation to
    pay whatever the far side is asking, and the depth walk that justified the
    basket assumed a price."""

    def __post_init__(self) -> None:
        if self.quantity <= ZERO:
            raise ValueError("an order for no contracts is not an order")
        if self.limit_price <= ZERO:
            raise ValueError("a limit price must be positive")
        if not self.idempotency_key:
            raise ValueError(
                "an order without an idempotency key cannot be retried safely, and an "
                "order that cannot be retried safely must not be sent"
            )


@dataclass(frozen=True, slots=True)
class OrderResult:
    """What came back."""

    outcome: OrderOutcome
    filled: Decimal = ZERO
    cost: Decimal = ZERO
    """What was actually paid, at the prices actually filled."""

    venue_order_id: str | None = None
    detail: str = ""

    @property
    def is_certain(self) -> bool:
        return self.outcome is not OrderOutcome.UNKNOWN


class OrderGateway(Protocol):
    """The venue operations an executor needs. Implementations decide nothing."""

    async def place(self, request: OrderRequest) -> OrderResult:
        """Submit one leg. Must be idempotent on ``request.idempotency_key``."""
        ...

    async def cancel(self, idempotency_key: str) -> bool:
        """Cancel a resting order. Returns whether anything was cancelled."""
        ...

    async def sell(self, request: OrderRequest) -> OrderResult:
        """Close a position, for unwinding a basket that cannot be completed."""
        ...


@dataclass(slots=True)
class PaperGateway:
    """An in-memory venue that fills at the limit price.

    Not a simulation of the market -- :mod:`arbbot.shadow` already models
    latency, vanishing levels and partial fills, and this deliberately does not
    duplicate it. This exists so the executor's control flow can be tested
    exhaustively: which leg was attempted, in what order, what happened after
    the third one failed, whether a retry reused its key.

    Its defaults are generous on purpose. A paper gateway that filled
    realistically would make executor tests depend on the fill model, and then
    a change to the fill model would break tests about unwind logic.
    """

    fill_ratio: Decimal = Decimal("1")
    """Fraction of each request that fills. Below one produces partials."""

    reject: set[str] = field(default_factory=set)
    """Tickers to refuse outright."""

    vanish: set[str] = field(default_factory=set)
    """Tickers whose response is lost -- the uncertain case."""

    placed: list[OrderRequest] = field(default_factory=list)
    sold: list[OrderRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    _by_key: dict[str, OrderResult] = field(default_factory=dict)

    async def place(self, request: OrderRequest) -> OrderResult:
        # Idempotency is honoured rather than assumed. A gateway that quietly
        # filled a repeated key twice would hide exactly the bug the key exists
        # to prevent, and the executor's retry tests would pass while the real
        # venue doubled the position.
        if request.idempotency_key in self._by_key:
            return self._by_key[request.idempotency_key]

        self.placed.append(request)
        if request.ticker in self.reject:
            result = OrderResult(OrderOutcome.REJECTED, detail=f"{request.ticker} refused")
        elif request.ticker in self.vanish:
            result = OrderResult(OrderOutcome.UNKNOWN, detail=f"{request.ticker} did not answer")
        else:
            filled = (request.quantity * self.fill_ratio).quantize(Decimal("0.01"))
            result = OrderResult(
                outcome=(
                    OrderOutcome.FILLED if filled >= request.quantity else OrderOutcome.PARTIAL
                ),
                filled=filled,
                cost=filled * request.limit_price,
                venue_order_id=f"paper-{len(self.placed)}",
            )

        # UNKNOWN is not recorded against the key. The whole point of that
        # outcome is that this side does not know what happened, so replaying
        # the key must reach the venue again rather than replay a guess.
        if result.outcome is not OrderOutcome.UNKNOWN:
            self._by_key[request.idempotency_key] = result
        return result

    async def cancel(self, idempotency_key: str) -> bool:
        self.cancelled.append(idempotency_key)
        return idempotency_key in self._by_key

    async def sell(self, request: OrderRequest) -> OrderResult:
        self.sold.append(request)
        if request.ticker in self.vanish:
            return OrderResult(OrderOutcome.UNKNOWN, detail="unwind did not answer")
        return OrderResult(
            OrderOutcome.FILLED,
            filled=request.quantity,
            cost=request.quantity * request.limit_price,
            venue_order_id=f"paper-sell-{len(self.sold)}",
        )
