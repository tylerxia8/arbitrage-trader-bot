"""Rejection reason catalog.

The catalog is asserted literally against the specification. If someone renames
a code, this test fails and forces them to update the daily report grouping and
the appendix at the same time -- which is the point, since a silently renamed
code makes historical rejection counts incomparable.
"""

from __future__ import annotations

from arbbot.reasons import RejectionReason

SPECIFIED_CATALOG = {
    "relationship_not_approved",
    "terms_changed",
    "market_not_open",
    "book_incomplete",
    "stale_quote",
    "insufficient_depth",
    "unknown_fee",
    "nonpositive_net_edge",
    "risk_limit",
    "approval_expired",
    "duplicate_intent",
    "order_state_unknown",
    "reconciliation_difference",
}


class TestCatalog:
    def test_matches_the_specification_exactly(self) -> None:
        assert {r.value for r in RejectionReason} == SPECIFIED_CATALOG

    def test_every_code_is_documented(self) -> None:
        for reason in RejectionReason:
            assert reason.__doc__, f"{reason.name} has no docstring"

    def test_codes_are_stable_strings(self) -> None:
        """Persisted values must be plain snake_case, never the enum repr."""
        for reason in RejectionReason:
            assert reason.value == reason.value.lower()
            assert " " not in reason.value


class TestHaltingReasons:
    def test_model_inconsistency_halts_trading(self) -> None:
        """These three mean the system's picture of the world is wrong.
        Continuing to trade on a wrong picture turns a bounded loss into an
        unbounded one."""
        halting = {r for r in RejectionReason if r.halts_trading}
        assert halting == {
            RejectionReason.ORDER_STATE_UNKNOWN,
            RejectionReason.RECONCILIATION_DIFFERENCE,
            RejectionReason.TERMS_CHANGED,
        }

    def test_ordinary_rejections_do_not_halt_trading(self) -> None:
        """A thin or stale book means 'not this one', not 'stop everything'."""
        for reason in (
            RejectionReason.STALE_QUOTE,
            RejectionReason.INSUFFICIENT_DEPTH,
            RejectionReason.NONPOSITIVE_NET_EDGE,
            RejectionReason.UNKNOWN_FEE,
        ):
            assert not reason.halts_trading, reason
