"""Structural classification of event groups.

The enumerated-vs-partition distinction is the whole point. Getting it wrong
in the permissive direction means proposing baskets that are not exhaustive,
which look cheap precisely because they can pay nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbbot.venues.kalshi.discovery import StructuralVerdict, classify_event


def market(ticker: str, strike_type: str | None = "between") -> dict[str, Any]:
    return {"ticker": ticker, "strike_type": strike_type}


def event(exclusive: bool = True, ticker: str = "KXTEST-1") -> dict[str, Any]:
    return {"event_ticker": ticker, "mutually_exclusive": exclusive}


def partition_markets(buckets: int = 4) -> list[dict[str, Any]]:
    legs = [market("LOW", "less")]
    legs += [market(f"MID{i}", "between") for i in range(buckets)]
    legs.append(market("HIGH", "greater"))
    return legs


class TestPartitions:
    def test_numeric_buckets_with_both_tails_are_a_partition(self) -> None:
        result = classify_event(event(), partition_markets())
        assert result.verdict is StructuralVerdict.PARTITION
        assert result.may_propose

    def test_a_real_weather_event_classifies_as_a_partition(self) -> None:
        """Shape captured live from KXHIGHTATL-26AUG13."""
        legs = [
            market("T91", "less"),
            market("T92", "between"),
            market("T94", "between"),
            market("T96", "between"),
            market("T98", "between"),
            market("T100", "greater"),
        ]
        assert classify_event(event(), legs).verdict is StructuralVerdict.PARTITION

    def test_proposing_is_recorded_as_review_not_approval(self) -> None:
        """A drafted relationship still enters the registry as PENDING."""
        assert "human review" in classify_event(event(), partition_markets()).reason


class TestTheEnumeratedTrap:
    def test_named_candidates_are_not_a_partition(self) -> None:
        """The failure this module exists to prevent. Sixteen listed 2028
        matchups priced at $0.746 in a live survey -- an apparent 25% edge
        that is really the market pricing the outcomes nobody listed."""
        legs = [market(f"C{i}", "custom") for i in range(16)]
        result = classify_event(event(), legs)

        assert result.verdict is StructuralVerdict.ENUMERATED
        assert not result.may_propose
        assert "exhaustive" in result.reason

    def test_exclusivity_alone_does_not_make_a_basket(self) -> None:
        """Kalshi's mutually_exclusive flag means *at most* one wins. A basket
        needs exactly one, which also requires at least one."""
        legs = [market(f"C{i}", "custom") for i in range(5)]
        assert classify_event(event(exclusive=True), legs).verdict is StructuralVerdict.ENUMERATED


class TestIncompleteCoverage:
    def test_a_missing_upper_tail_is_open_ended(self) -> None:
        legs = [market("LOW", "less"), market("MID", "between"), market("MID2", "between")]
        result = classify_event(event(), legs)
        assert result.verdict is StructuralVerdict.OPEN_ENDED
        assert "upper" in result.reason

    def test_a_missing_lower_tail_is_open_ended(self) -> None:
        legs = [market("MID", "between"), market("MID2", "between"), market("HIGH", "greater")]
        assert "lower" in classify_event(event(), legs).reason

    def test_only_middle_buckets_names_both_missing_tails(self) -> None:
        legs = [market(f"MID{i}", "between") for i in range(4)]
        result = classify_event(event(), legs)
        assert result.verdict is StructuralVerdict.OPEN_ENDED
        assert "lower" in result.reason
        assert "upper" in result.reason


class TestRejections:
    def test_non_exclusive_events_are_not_partitions(self) -> None:
        """Without exclusivity two legs can both pay, so the set is not a
        partition however numeric its buckets are."""
        result = classify_event(event(exclusive=False), partition_markets())
        assert result.verdict is StructuralVerdict.MIXED
        assert not result.may_propose

    def test_mixed_strike_types_are_unclassifiable(self) -> None:
        legs = [market("LOW", "less"), market("NAMED", "custom"), market("HIGH", "greater")]
        assert classify_event(event(), legs).verdict is StructuralVerdict.MIXED

    def test_unrecognised_strike_types_are_unclassifiable(self) -> None:
        legs = [market(f"S{i}", "structured") for i in range(4)]
        result = classify_event(event(), legs)
        assert result.verdict is StructuralVerdict.MIXED
        assert "structured" in result.reason

    def test_missing_strike_types_are_unclassifiable(self) -> None:
        legs = [market(f"S{i}", None) for i in range(4)]
        assert classify_event(event(), legs).verdict is StructuralVerdict.MIXED

    def test_too_few_outcomes(self) -> None:
        legs = [market("LOW", "less"), market("HIGH", "greater")]
        assert classify_event(event(), legs).verdict is StructuralVerdict.TOO_FEW


class TestVerdictPolicy:
    def test_only_partitions_may_be_proposed(self) -> None:
        proposable = {v for v in StructuralVerdict if v.may_propose}
        assert proposable == {StructuralVerdict.PARTITION}

    @pytest.mark.parametrize("verdict", list(StructuralVerdict))
    def test_every_verdict_is_documented(self, verdict: StructuralVerdict) -> None:
        assert verdict.__doc__


class TestReporting:
    def test_records_the_outcome_tickers(self) -> None:
        """The proposal has to name the legs a reviewer will read terms for."""
        result = classify_event(event(), partition_markets(buckets=2))
        assert result.outcomes == 4
        assert result.tickers == ("LOW", "MID0", "MID1", "HIGH")
