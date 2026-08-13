"""The shadow capital ledger (FR-013).

Written as attacks on the three invariants, because each one is a way a
simulated strategy could report profits it never had: an unbalanced
transaction, an overdraft, or two candidates spending the same dollar.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from arbbot.ledger import AccountKind, CapitalLedger, LedgerError, Posting, Transaction

D = Decimal
T0 = dt.datetime(2026, 8, 13, 12, 0, tzinfo=dt.UTC)


def funded(amount: str = "1000") -> CapitalLedger:
    ledger = CapitalLedger()
    ledger.deposit(D(amount), at=T0)
    return ledger


class TestBalancing:
    def test_a_deposit_is_an_ordinary_balanced_transaction(self) -> None:
        """Capital paid in is not profit, however convenient that would be."""
        ledger = funded("500")
        assert ledger.cash == D("500")
        assert ledger.realized_pnl == D("0")
        assert ledger.balance(AccountKind.OPENING_CAPITAL) == D("-500")

    def test_an_unbalanced_transaction_is_refused(self) -> None:
        """Refused before writing, so the ledger cannot drift into a state
        that has to be reconciled by judgement."""
        ledger = funded()
        with pytest.raises(LedgerError, match="does not balance"):
            ledger.post(
                Transaction(
                    reference="bad",
                    at=T0,
                    postings=(Posting(AccountKind.CASH, D("-10")),),
                )
            )

    def test_every_transaction_sums_to_zero(self) -> None:
        ledger = funded()
        ledger.reserve(D("100"), at=T0, reference="r1")
        ledger.fill("X", contracts=D("10"), cost=D("80"), fee=D("2"), at=T0, reference="f1")

        for transaction in ledger.transactions:
            assert transaction.total == D("0")
        ledger.check()


class TestOverdraft:
    def test_cash_cannot_go_negative(self) -> None:
        """A shadow ledger permitting an overdraft simulates a credit facility
        nobody has, and would report profits that depended on it."""
        ledger = funded("10")
        with pytest.raises(LedgerError, match="overdraw"):
            ledger.reserve(D("50"), at=T0, reference="too-much")

    def test_reserves_cannot_be_over_released(self) -> None:
        ledger = funded()
        ledger.reserve(D("100"), at=T0, reference="r")
        with pytest.raises(LedgerError, match="over-release"):
            ledger.release(D("500"), at=T0, reference="r")

    def test_a_deposit_must_be_positive(self) -> None:
        with pytest.raises(LedgerError, match="positive"):
            CapitalLedger().deposit(D("-5"), at=T0)


class TestDoubleSpending:
    def test_reserved_funds_leave_cash(self) -> None:
        """The window between committing to a basket and finishing it is
        exactly when a second candidate would spend the same dollar."""
        ledger = funded("100")
        ledger.reserve(D("60"), at=T0, reference="basket-1")

        assert ledger.cash == D("40")
        assert ledger.reserved == D("60")

    def test_a_second_basket_cannot_use_reserved_cash(self) -> None:
        ledger = funded("100")
        ledger.reserve(D("60"), at=T0, reference="basket-1")
        with pytest.raises(LedgerError, match="overdraw"):
            ledger.reserve(D("60"), at=T0, reference="basket-2")

    def test_releasing_returns_the_money(self) -> None:
        ledger = funded("100")
        ledger.reserve(D("60"), at=T0, reference="b")
        ledger.release(D("60"), at=T0, reference="b")

        assert ledger.cash == D("100")
        assert ledger.reserved == D("0")


class TestFillsAndSettlement:
    def test_a_fill_moves_reserved_cash_into_a_position(self) -> None:
        ledger = funded("100")
        ledger.reserve(D("50"), at=T0, reference="b")
        ledger.fill("X", contracts=D("10"), cost=D("40"), fee=D("2"), at=T0, reference="b")

        assert ledger.position("X") == D("40")
        assert ledger.balance(AccountKind.FEES) == D("2")
        assert ledger.reserved == D("8")

    def test_settlement_books_the_difference_to_pnl(self) -> None:
        """Bought ten contracts for $4.00 that settle at $10.00."""
        ledger = funded("100")
        ledger.reserve(D("10"), at=T0, reference="b")
        ledger.fill("X", contracts=D("10"), cost=D("4"), fee=D("0"), at=T0, reference="b")
        ledger.settle("X", proceeds=D("10"), at=T0, reference="s")

        assert ledger.position("X") == D("0")
        assert ledger.realized_pnl == D("6")

    def test_fees_reduce_reported_profit(self) -> None:
        """A strategy profitable except for its costs is not profitable."""
        ledger = funded("100")
        ledger.reserve(D("10"), at=T0, reference="b")
        ledger.fill("X", contracts=D("10"), cost=D("4"), fee=D("1"), at=T0, reference="b")
        ledger.settle("X", proceeds=D("10"), at=T0, reference="s")

        assert ledger.realized_pnl == D("5")

    def test_settling_nothing_is_refused(self) -> None:
        with pytest.raises(LedgerError, match="no position"):
            funded().settle("X", proceeds=D("1"), at=T0, reference="s")


class TestUnwind:
    def test_an_unwind_is_booked_separately_from_profit(self) -> None:
        """A strategy that is profitable except for its unwinds is not
        profitable, and merging them hides the failure mode multi-leg
        execution is most exposed to."""
        ledger = funded("100")
        ledger.reserve(D("10"), at=T0, reference="b")
        ledger.fill("X", contracts=D("10"), cost=D("5"), fee=D("0"), at=T0, reference="b")
        ledger.unwind("X", proceeds=D("4"), fee=D("0"), at=T0, reference="u")

        assert ledger.balance(AccountKind.UNWIND_LOSS) == D("1")
        assert ledger.realized_pnl == D("-1")
        assert ledger.position("X") == D("0")

    def test_unwinding_nothing_is_refused(self) -> None:
        with pytest.raises(LedgerError, match="no position"):
            funded().unwind("X", proceeds=D("1"), fee=D("0"), at=T0, reference="u")


class TestCapitalDays:
    def test_idle_capital_accrues_nothing(self) -> None:
        """Money sitting in cash is not committed, so it earns no capital-day
        charge -- otherwise every strategy looks equally capital-hungry."""
        ledger = funded("1000")
        ledger.deposit(D("1"), at=T0 + dt.timedelta(days=7), reference="d2")
        assert ledger.capital_days == D("0")

    def test_committed_capital_accrues(self) -> None:
        ledger = funded("1000")
        ledger.reserve(D("100"), at=T0, reference="b")
        # One full day later, something else happens and the clock is read.
        ledger.release(D("100"), at=T0 + dt.timedelta(days=1), reference="b")

        assert ledger.capital_days == D("100")

    def test_more_capital_for_longer_accrues_more(self) -> None:
        quick = funded("1000")
        quick.reserve(D("100"), at=T0, reference="b")
        quick.release(D("100"), at=T0 + dt.timedelta(hours=1), reference="b")

        slow = funded("1000")
        slow.reserve(D("100"), at=T0, reference="b")
        slow.release(D("100"), at=T0 + dt.timedelta(days=2), reference="b")

        assert slow.capital_days > quick.capital_days

    def test_capital_days_are_deterministic(self) -> None:
        """Accrued on events rather than a timer, so a replay gives the same
        number."""

        def run() -> Decimal:
            ledger = funded("1000")
            ledger.reserve(D("50"), at=T0, reference="b")
            ledger.release(D("50"), at=T0 + dt.timedelta(hours=6), reference="b")
            return ledger.capital_days

        assert run() == run()


class TestIntegrity:
    def test_check_passes_on_a_healthy_ledger(self) -> None:
        ledger = funded("100")
        ledger.reserve(D("10"), at=T0, reference="b")
        ledger.fill("X", contracts=D("5"), cost=D("8"), fee=D("1"), at=T0, reference="b")
        ledger.check()

    def test_committed_counts_reservations_and_positions(self) -> None:
        ledger = funded("100")
        ledger.reserve(D("50"), at=T0, reference="b")
        ledger.fill("X", contracts=D("10"), cost=D("30"), fee=D("0"), at=T0, reference="b")

        assert ledger.committed == D("50")  # 20 still reserved + 30 in position
