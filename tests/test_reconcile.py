"""Finding out what is held after the system stopped knowing.

``UNKNOWN`` halts the strategy and counts fully against every limit, and until
this existed there was no way out of it -- one uncertain response stopped the
system permanently. Every test here is about resolving that *without* inventing
a conclusion about real money.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import pytest

from arbbot.execution.reconcile import Reconciler, Verdict
from arbbot.states import OrderState, can_transition

D = Decimal
EXPECTED = {"A": D("10"), "B": D("10"), "C": D("10")}


class Venue:
    """A position source that can also be told to be unreachable."""

    def __init__(self, held: Mapping[str, Decimal] | None, *, reachable: bool = True) -> None:
        self.held = held
        self.reachable = reachable
        self.asked: list[list[str]] = []

    async def positions(self, tickers: list[str]) -> Mapping[str, Decimal] | None:
        self.asked.append(tickers)
        return dict(self.held or {}) if self.reachable else None


class TestVerdicts:
    async def test_every_leg_held_is_the_basket(self) -> None:
        report = await Reconciler(Venue(EXPECTED)).check("i1", EXPECTED)

        assert report.verdict is Verdict.HELD_IN_FULL
        assert report.next_state is OrderState.FILLED
        assert report.differences == {}

    async def test_nothing_held_is_a_clean_failure(self) -> None:
        report = await Reconciler(Venue({})).check("i1", EXPECTED)

        assert report.verdict is Verdict.NOTHING_HELD
        assert report.next_state is OrderState.FAILED

    async def test_a_partial_holding_is_an_incident(self) -> None:
        """Not a failure. Capital is exposed in a direction nobody chose, and
        the system got here by losing track -- so the one thing it must not do
        is automatically sell what it has just discovered it owns."""
        report = await Reconciler(Venue({"A": D("10"), "B": D("10")})).check("i1", EXPECTED)

        assert report.verdict is Verdict.PARTIAL
        assert report.next_state is OrderState.INCIDENT
        assert report.needs_human is True

    async def test_a_short_fill_on_one_leg_is_partial(self) -> None:
        held = {"A": D("10"), "B": D("10"), "C": D("4")}
        report = await Reconciler(Venue(held)).check("i1", EXPECTED)

        assert report.verdict is Verdict.PARTIAL
        assert report.differences == {"C": D("-6")}


class TestOverfill:
    async def test_holding_more_than_ordered_is_flagged(self) -> None:
        """The failure idempotency keys exist to prevent. It means a submission
        was duplicated."""
        held = {"A": D("20"), "B": D("10"), "C": D("10")}
        report = await Reconciler(Venue(held)).check("i1", EXPECTED)

        assert report.verdict is Verdict.OVERFILLED
        assert report.next_state is OrderState.INCIDENT
        assert report.differences == {"A": D("10")}

    async def test_a_position_in_an_unordered_ticker_is_flagged(self) -> None:
        held = {"A": D("10"), "B": D("10"), "C": D("10"), "STRAY": D("5")}
        report = await Reconciler(Venue(held)).check("i1", EXPECTED)

        assert report.verdict is Verdict.OVERFILLED

    async def test_the_report_warns_against_unwinding_first(self) -> None:
        """The same fault that duplicated the buy would duplicate the sell."""
        rendered = (
            await Reconciler(Venue({"A": D("20"), "B": D("10"), "C": D("10")})).check(
                "i1", EXPECTED
            )
        ).render()

        assert "submitted twice" in rendered
        assert "Do not unwind" in rendered


class TestUnavailable:
    async def test_an_unreachable_venue_concludes_nothing(self) -> None:
        """No answer is not evidence of no position. Guessing here is how a
        real position becomes an invisible one."""
        report = await Reconciler(Venue(None, reachable=False)).check("i1", EXPECTED)

        assert report.verdict is Verdict.UNAVAILABLE
        assert report.next_state is OrderState.UNKNOWN
        assert report.found == {}

    async def test_an_empty_answer_is_not_the_same_as_no_answer(self) -> None:
        """One is the venue saying "you hold nothing". The other is the venue
        saying nothing at all."""
        empty = await Reconciler(Venue({})).check("i1", EXPECTED)
        silent = await Reconciler(Venue(None, reachable=False)).check("i1", EXPECTED)

        assert empty.verdict is Verdict.NOTHING_HELD
        assert silent.verdict is Verdict.UNAVAILABLE
        assert empty.next_state is not silent.next_state


class TestMachineCompatibility:
    def test_every_resolution_is_a_legal_transition_from_reconciling(self) -> None:
        """The verdicts have to land where the state machine allows, or
        reconciliation cannot actually be applied."""
        for state in (OrderState.FILLED, OrderState.FAILED, OrderState.INCIDENT):
            assert can_transition(OrderState.RECONCILING, state)

    def test_unknown_only_leads_to_reconciling(self) -> None:
        """The reason this module had to exist: without it, UNKNOWN was a dead
        end and one uncertain response stopped the system permanently."""
        assert can_transition(OrderState.UNKNOWN, OrderState.RECONCILING)
        for state in OrderState:
            if state is not OrderState.RECONCILING:
                assert not can_transition(OrderState.UNKNOWN, state)

    @pytest.mark.parametrize("verdict", [Verdict.HELD_IN_FULL, Verdict.NOTHING_HELD])
    def test_resolving_verdicts_are_marked_as_such(self, verdict: Verdict) -> None:
        assert verdict.resolves is True

    @pytest.mark.parametrize("verdict", [Verdict.PARTIAL, Verdict.OVERFILLED, Verdict.UNAVAILABLE])
    def test_unresolved_verdicts_are_not(self, verdict: Verdict) -> None:
        assert verdict.resolves is False


class TestScope:
    async def test_reconciliation_does_not_trade(self) -> None:
        """It establishes what is true. Acting on that happens afterwards under
        the ordinary gates -- a reconciler that also traded would be taking
        positions from the state in which it just admitted not knowing what it
        held."""
        venue = Venue({"A": D("10")})
        await Reconciler(venue).check("i1", EXPECTED)

        assert not hasattr(venue, "placed"), "the position source cannot place orders"
        assert venue.asked == [["A", "B", "C"]], "it only ever asks"
