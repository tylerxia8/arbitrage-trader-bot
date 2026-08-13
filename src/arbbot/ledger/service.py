"""The shadow capital ledger (FR-013, EPIC-12).

Tracks what the strategy would have held, spent, and earned, under
double-entry discipline. No real money moves; the point is that when real
money eventually does, the accounting has already been exercised against
months of simulated fills rather than written fresh on the day it matters.

Three invariants are enforced on every operation rather than checked
afterwards:

**Entries balance.** Every transaction sums to zero. A transaction that does
not balance is refused before it is written, so the ledger cannot drift into
a state that has to be reconciled by judgement.

**Cash cannot go negative.** A shadow ledger permitting an overdraft is
simulating a credit facility nobody has, and it would report profits that
depended on it.

**Reserved funds are not spendable.** A basket is acquired leg by leg, and the
window between committing to it and finishing it is exactly when a second
candidate would otherwise be evaluated against the same dollar. Reserving
first is what makes "prevents double spending" true rather than aspirational.

Capital-days are accumulated as capital is committed and released, because
"return per capital-day" is the metric the specification uses to decide
whether the strategy beats leaving the money alone -- and a percentage return
on capital that sat idle most of the week is not comparable to anything.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from arbbot.ledger.accounts import POSITION_PREFIX, AccountKind, position_account
from arbbot.money import ZERO, quantize_cost, to_usd

__all__ = ["CapitalLedger", "LedgerError", "Posting", "Transaction"]


class LedgerError(RuntimeError):
    """An operation the ledger refuses. Always a bug, never a market event."""


@dataclass(frozen=True, slots=True)
class Posting:
    """One side of a transaction. Debit positive, credit negative."""

    account: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Transaction:
    """A balanced set of postings."""

    reference: str
    postings: tuple[Posting, ...]
    at: dt.datetime
    memo: str = ""

    @property
    def total(self) -> Decimal:
        return sum((p.amount for p in self.postings), ZERO)


@dataclass(slots=True)
class CapitalLedger:
    """In-memory double-entry ledger for shadow execution.

    Deliberately not persisted here. Milestone 3 replays the archive many
    times over with different assumptions, and a ledger that wrote to the
    database on every simulated fill would make each run a migration event
    rather than an experiment.
    """

    balances: dict[str, Decimal] = field(default_factory=dict)
    transactions: list[Transaction] = field(default_factory=list)
    capital_days: Decimal = ZERO
    _committed_since: dt.datetime | None = field(default=None, repr=False)

    # -- reads -----------------------------------------------------------
    def balance(self, account: str) -> Decimal:
        return self.balances.get(account, ZERO)

    @property
    def cash(self) -> Decimal:
        return self.balance(AccountKind.CASH)

    @property
    def reserved(self) -> Decimal:
        return self.balance(AccountKind.RESERVED)

    @property
    def available(self) -> Decimal:
        """Cash that may actually be committed to something new."""
        return self.cash

    @property
    def committed(self) -> Decimal:
        """Capital currently tied up: reserved plus everything held."""
        return self.reserved + sum(
            (amount for name, amount in self.balances.items() if name.startswith(POSITION_PREFIX)),
            ZERO,
        )

    @property
    def realized_pnl(self) -> Decimal:
        """Settled profit, net of fees and unwind losses.

        Negated because of the sign convention: debits are positive, so an
        income account carries a *credit* balance and a profitable settlement
        leaves ``REALIZED_PNL`` negative. Expenses are debits, so fees and
        unwinds subtract directly.

        Those two are separate accounts precisely so this number cannot
        quietly exclude them. A strategy profitable except for its costs is
        not profitable.
        """
        return (
            -self.balance(AccountKind.REALIZED_PNL)
            - self.balance(AccountKind.FEES)
            - self.balance(AccountKind.UNWIND_LOSS)
        )

    def position(self, ticker: str) -> Decimal:
        return self.balance(position_account(ticker))

    # -- writing ---------------------------------------------------------
    def post(self, transaction: Transaction) -> None:
        """Apply a balanced transaction, or refuse it."""
        if not transaction.postings:
            raise LedgerError("a transaction needs postings")
        if transaction.total != ZERO:
            raise LedgerError(
                f"transaction {transaction.reference!r} does not balance "
                f"(sums to {transaction.total}); refused before writing so the "
                f"ledger cannot drift into a state reconciled by judgement"
            )

        projected = dict(self.balances)
        for posting in transaction.postings:
            projected[posting.account] = projected.get(posting.account, ZERO) + posting.amount

        if projected.get(AccountKind.CASH, ZERO) < ZERO:
            raise LedgerError(
                f"transaction {transaction.reference!r} would overdraw cash to "
                f"{projected[AccountKind.CASH]}; a shadow ledger that permits an "
                f"overdraft simulates a facility nobody has"
            )
        if projected.get(AccountKind.RESERVED, ZERO) < ZERO:
            raise LedgerError(f"transaction {transaction.reference!r} would over-release reserves")

        self._accrue_capital_days(transaction.at)
        self.balances = projected
        self.transactions.append(transaction)

    def _accrue_capital_days(self, at: dt.datetime) -> None:
        """Add capital-days for the stretch just ended, then restart the clock.

        Accrued on each transaction rather than on a timer, so the measure
        depends only on the sequence of events and stays identical on replay.
        """
        committed = self.committed
        if self._committed_since is not None and committed > ZERO:
            elapsed = (at - self._committed_since).total_seconds()
            self.capital_days += committed * Decimal(elapsed) / Decimal(86400)
        self._committed_since = at

    # -- operations ------------------------------------------------------
    def deposit(self, amount: Decimal, *, at: dt.datetime, reference: str = "deposit") -> None:
        """Fund the shadow account."""
        value = to_usd(amount)
        if value <= ZERO:
            raise LedgerError("a deposit must be positive")
        self.post(
            Transaction(
                reference=reference,
                at=at,
                postings=(
                    Posting(AccountKind.CASH, value),
                    Posting(AccountKind.OPENING_CAPITAL, -value),
                ),
                memo="opening capital",
            )
        )

    def reserve(self, amount: Decimal, *, at: dt.datetime, reference: str) -> None:
        """Earmark cash for an intent that has not filled yet."""
        value = to_usd(amount)
        if value <= ZERO:
            raise LedgerError("a reservation must be positive")
        self.post(
            Transaction(
                reference=reference,
                at=at,
                postings=(
                    Posting(AccountKind.CASH, -value),
                    Posting(AccountKind.RESERVED, value),
                ),
                memo="reserved for intent",
            )
        )

    def release(self, amount: Decimal, *, at: dt.datetime, reference: str) -> None:
        """Return unspent reserved funds to cash."""
        value = to_usd(amount)
        self.post(
            Transaction(
                reference=reference,
                at=at,
                postings=(
                    Posting(AccountKind.RESERVED, -value),
                    Posting(AccountKind.CASH, value),
                ),
                memo="reservation released",
            )
        )

    def fill(
        self,
        ticker: str,
        *,
        contracts: Decimal,
        cost: Decimal,
        fee: Decimal,
        at: dt.datetime,
        reference: str,
        from_reserved: bool = True,
    ) -> None:
        """Record acquiring ``contracts`` of ``ticker``.

        Funds come from the reservation by default, because that is the whole
        point of reserving: the money was already set aside for this intent.
        """
        cost = quantize_cost(cost)
        fee = quantize_cost(fee)
        outlay = cost + fee
        source = AccountKind.RESERVED if from_reserved else AccountKind.CASH

        self.post(
            Transaction(
                reference=reference,
                at=at,
                postings=(
                    Posting(source, -outlay),
                    Posting(position_account(ticker), cost),
                    Posting(AccountKind.FEES, fee),
                ),
                memo=f"filled {contracts} {ticker}",
            )
        )

    def settle(
        self,
        ticker: str,
        *,
        proceeds: Decimal,
        at: dt.datetime,
        reference: str,
    ) -> None:
        """Close a position at settlement, booking the difference to P&L."""
        held = self.position(ticker)
        if held <= ZERO:
            raise LedgerError(f"no position in {ticker} to settle")

        value = to_usd(proceeds)
        self.post(
            Transaction(
                reference=reference,
                at=at,
                postings=(
                    Posting(AccountKind.CASH, value),
                    Posting(position_account(ticker), -held),
                    Posting(AccountKind.REALIZED_PNL, held - value),
                ),
                memo=f"settled {ticker}",
            )
        )

    def unwind(
        self,
        ticker: str,
        *,
        proceeds: Decimal,
        fee: Decimal,
        at: dt.datetime,
        reference: str,
    ) -> None:
        """Close a position early, booking the shortfall as an unwind loss."""
        held = self.position(ticker)
        if held <= ZERO:
            raise LedgerError(f"no position in {ticker} to unwind")

        value = to_usd(proceeds)
        fee = quantize_cost(fee)
        self.post(
            Transaction(
                reference=reference,
                at=at,
                postings=(
                    Posting(AccountKind.CASH, value - fee),
                    Posting(position_account(ticker), -held),
                    Posting(AccountKind.UNWIND_LOSS, held - value),
                    Posting(AccountKind.FEES, fee),
                ),
                memo=f"unwound {ticker}",
            )
        )

    # -- integrity -------------------------------------------------------
    def check(self) -> None:
        """Verify every invariant. Raises on the first violation."""
        for transaction in self.transactions:
            if transaction.total != ZERO:
                raise LedgerError(f"transaction {transaction.reference!r} does not balance")
        if self.cash < ZERO:
            raise LedgerError(f"cash is negative: {self.cash}")
        if self.reserved < ZERO:
            raise LedgerError(f"reserved is negative: {self.reserved}")
