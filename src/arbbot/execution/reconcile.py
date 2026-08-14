"""Finding out what is actually held, after the system stopped knowing.

``UNKNOWN`` is the state an intent reaches when the venue did not answer
usefully. It is deliberately the most restrictive state in the machine: it
halts the strategy, it counts fully against every exposure limit, and the only
transition out of it is to ``RECONCILING``. Until now nothing implemented that
transition, which meant a single uncertain response stopped the system
permanently. That converts a recoverable condition into an outage, and it is
the gap this closes.

The rule the whole module follows: **reconciliation establishes what is true,
it does not repair anything.** It compares what the venue says is held against
what this system attempted, and produces a verdict. Acting on that verdict --
unwinding a stray position, completing a basket -- happens afterwards, under
the ordinary gates, with the ordinary limits. A reconciler that also traded
would be taking positions from precisely the state in which it had just
admitted not knowing what it held.

Three consequences worth stating.

**An unreachable venue leaves the intent exactly where it was.** No answer is
not evidence of no position. If positions cannot be fetched, the verdict is
that reconciliation could not be performed, and the intent stays ``UNKNOWN``.
Guessing here is how a real position becomes an invisible one.

**A partial holding is an incident, not a failure.** ``FAILED`` means the
intent ended without the intended basket and without a position. If some legs
are held, capital is exposed and the direction was never chosen -- and the
system arrived here by losing track, so the one thing it should not do is
automatically sell things it has just discovered it owns. A human decides
whether to unwind or complete.

**Holding more than was intended is the worst case, and is called out.** It
means an order was submitted twice, which is the failure idempotency keys exist
to prevent. It is an incident with a specific, loud detail, because the
response is not "unwind the extra" but "find out how the key was reused before
anything else is sent".
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from arbbot.money import ZERO
from arbbot.states import OrderState

__all__ = [
    "PositionSource",
    "Reconciler",
    "ReconciliationReport",
    "Verdict",
]


class Verdict(enum.StrEnum):
    """What reconciliation concluded."""

    HELD_IN_FULL = "held_in_full"
    """Every leg is present at the intended size. The basket exists."""

    NOTHING_HELD = "nothing_held"
    """No leg is present. The intent left no position behind."""

    PARTIAL = "partial"
    """Some legs are held. Capital is exposed in a direction nobody chose."""

    OVERFILLED = "overfilled"
    """More is held than was ever intended -- an order was sent twice."""

    UNAVAILABLE = "unavailable"
    """Positions could not be read. Nothing is concluded."""

    @property
    def resolves(self) -> bool:
        """Whether this verdict is enough to leave ``RECONCILING``."""
        return self in (Verdict.HELD_IN_FULL, Verdict.NOTHING_HELD)


class PositionSource(Protocol):
    """Where the truth about held positions comes from -- the venue."""

    async def positions(self, tickers: list[str]) -> Mapping[str, Decimal] | None:
        """Quantity held per ticker, or ``None`` if it cannot be determined.

        ``None`` rather than an empty mapping when the venue is unreachable.
        An empty mapping is a claim -- "you hold nothing" -- and a failure to
        answer is not that claim.
        """
        ...


@dataclass(slots=True)
class ReconciliationReport:
    """What was found, what was expected, and what follows."""

    intent_id: str
    verdict: Verdict
    expected: dict[str, Decimal] = field(default_factory=dict)
    found: dict[str, Decimal] = field(default_factory=dict)
    detail: str = ""

    @property
    def differences(self) -> dict[str, Decimal]:
        """Per ticker: held minus intended. Positive means more than expected."""
        tickers = set(self.expected) | set(self.found)
        return {
            ticker: self.found.get(ticker, ZERO) - self.expected.get(ticker, ZERO)
            for ticker in sorted(tickers)
            if self.found.get(ticker, ZERO) != self.expected.get(ticker, ZERO)
        }

    @property
    def next_state(self) -> OrderState:
        """Where the intent should go.

        ``UNAVAILABLE`` returns ``UNKNOWN``: staying put is the correct
        response to having learned nothing, and the alternative is inventing a
        conclusion about real money.
        """
        match self.verdict:
            case Verdict.HELD_IN_FULL:
                return OrderState.FILLED
            case Verdict.NOTHING_HELD:
                return OrderState.FAILED
            case Verdict.UNAVAILABLE:
                return OrderState.UNKNOWN
            case _:
                return OrderState.INCIDENT

    @property
    def needs_human(self) -> bool:
        return self.next_state is OrderState.INCIDENT

    def render(self) -> str:
        lines = [
            f"intent   : {self.intent_id}",
            f"verdict  : {self.verdict.value}",
            f"next     : {self.next_state.value}",
        ]
        if self.detail:
            lines.append(f"detail   : {self.detail}")
        if self.differences:
            lines.append("differences (held - intended):")
            for ticker, delta in self.differences.items():
                lines.append(f"  {ticker:<32} {delta:+}")
        if self.verdict is Verdict.OVERFILLED:
            lines.append("")
            lines.append("MORE IS HELD THAN WAS ORDERED. An order was submitted twice, which")
            lines.append("is the failure idempotency keys exist to prevent. Do not unwind the")
            lines.append("excess until the duplicate submission is understood: the same fault")
            lines.append("would duplicate the unwind.")
        return "\n".join(lines)


class Reconciler:
    """Compares the venue's positions against what an intent attempted."""

    def __init__(self, source: PositionSource) -> None:
        self._source = source

    async def check(self, intent_id: str, expected: Mapping[str, Decimal]) -> ReconciliationReport:
        """Establish what is held for one intent.

        :param expected: intended quantity per leg. Taken as an argument rather
            than read from the store so this is usable during a manual
            investigation, where the store may itself be what is in doubt.
        """
        wanted = {ticker: qty for ticker, qty in expected.items()}
        found = await self._source.positions(sorted(wanted))

        if found is None:
            return ReconciliationReport(
                intent_id,
                Verdict.UNAVAILABLE,
                expected=dict(wanted),
                detail=(
                    "positions could not be read; no answer is not evidence of no "
                    "position, so the intent stays where it was"
                ),
            )

        held = {ticker: Decimal(qty) for ticker, qty in found.items() if Decimal(qty) != ZERO}
        report = ReconciliationReport(
            intent_id, Verdict.NOTHING_HELD, expected=dict(wanted), found=held
        )

        if any(held.get(t, ZERO) > qty for t, qty in wanted.items()) or (set(held) - set(wanted)):
            report.verdict = Verdict.OVERFILLED
            report.detail = "more is held than was ordered; a submission was duplicated"
            return report

        present = {t: q for t, q in held.items() if q > ZERO}
        if not present:
            report.verdict = Verdict.NOTHING_HELD
            report.detail = "no leg of this intent is held; it left nothing behind"
            return report

        if all(held.get(t, ZERO) == qty for t, qty in wanted.items()):
            report.verdict = Verdict.HELD_IN_FULL
            report.detail = "every leg is present at the intended size"
            return report

        report.verdict = Verdict.PARTIAL
        report.detail = (
            f"{len(present)} of {len(wanted)} legs held; capital is exposed in a "
            f"direction nobody chose, and this system arrived here by losing track -- "
            f"a person decides whether to unwind or complete"
        )
        return report
