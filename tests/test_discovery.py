"""Structural classification of event groups.

The enumerated-vs-partition distinction is the whole point. Getting it wrong
in the permissive direction means proposing baskets that are not exhaustive,
which look cheap precisely because they can pay nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbbot.venues.kalshi.discovery import (
    StructuralVerdict,
    check_integer_coverage,
    classify_event,
)


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


def bucket(strike_type: str, floor: str | None = None, cap: str | None = None) -> dict[str, Any]:
    return {
        "ticker": f"{strike_type}-{floor}-{cap}",
        "strike_type": strike_type,
        "floor_strike": floor,
        "cap_strike": cap,
    }


#: The real Atlanta high-temperature set, read off the live API on 2026-08-13.
#: Rules: "<92", "92-93", "94-95", "96-97", "98-99", ">99".
ATLANTA = [
    bucket("less", None, "92"),
    bucket("between", "92", "93"),
    bucket("between", "94", "95"),
    bucket("between", "96", "97"),
    bucket("between", "98", "99"),
    bucket("greater", "99", None),
]


class TestIntegerCoverage:
    def test_the_real_weather_set_tiles_the_integers(self) -> None:
        """The floor/cap fields look like they overlap -- "91 or below" carries
        cap=92 while the next bucket has floor=92 -- but the settlement rules
        say the lower tail is *strictly* below 92. Under that convention the
        set tiles the integers exactly."""
        report = check_integer_coverage(ATLANTA)
        assert report.covered, report.problems

    def test_a_missing_middle_bucket_is_a_gap(self) -> None:
        without_96_97 = [m for m in ATLANTA if m["floor_strike"] != "96"]
        report = check_integer_coverage(without_96_97)
        assert not report.covered
        assert any("gap" in p for p in report.problems)

    def test_an_overlapping_bucket_is_caught(self) -> None:
        overlapping = [
            bucket("less", None, "92"),
            bucket("between", "92", "94"),
            bucket("between", "94", "95"),
            bucket("greater", "95", None),
        ]
        report = check_integer_coverage(overlapping)
        assert not report.covered
        assert any("overlap" in p for p in report.problems)

    def test_a_detached_lower_tail_is_caught(self) -> None:
        """If the tail stopped at 91 while the first bucket started at 92, the
        integer 91 would resolve nothing -- and a basket that omits an outcome
        pays zero when it occurs."""
        detached = [bucket("less", None, "91"), *ATLANTA[1:]]
        report = check_integer_coverage(detached)
        assert not report.covered

    def test_a_detached_upper_tail_is_caught(self) -> None:
        detached = [*ATLANTA[:-1], bucket("greater", "101", None)]
        assert not check_integer_coverage(detached).covered

    def test_missing_tails_are_reported_not_assumed(self) -> None:
        report = check_integer_coverage(ATLANTA[1:-1])
        assert not report.covered
        assert "tail" in report.summary

    def test_a_bucket_without_strikes_is_unverifiable(self) -> None:
        broken = [ATLANTA[0], bucket("between", None, None), ATLANTA[-1]]
        assert not check_integer_coverage(broken).covered

    def test_summary_reads_cleanly_when_covered(self) -> None:
        assert check_integer_coverage(ATLANTA).summary == "tiles the integers"


class TestReporting:
    def test_records_the_outcome_tickers(self) -> None:
        """The proposal has to name the legs a reviewer will read terms for."""
        result = classify_event(event(), partition_markets(buckets=2))
        assert result.outcomes == 4
        assert result.tickers == ("LOW", "MID0", "MID1", "HIGH")
