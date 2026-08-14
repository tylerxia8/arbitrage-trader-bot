"""Turning venue structure into something a person can sign for.

The line every test here defends is that **proposing is not approving**.
Everything the proposal path does is automatic, and none of it establishes
that a basket is exhaustive. Only a reviewer does.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from arbbot.registry import (
    RegistryError,
    RelationshipRegistry,
    approve_group,
    fingerprint_of,
    group_pending,
    pending,
    propose_from_events,
    review_fingerprint,
    slug_for,
)
from arbbot.relationships import RelationshipStatus

EVENT_TICKER = "KXHIGHTEST-26AUG14"


def market(
    suffix: str,
    *,
    strike_type: str,
    floor: str | None = None,
    cap: str | None = None,
    rules: str = "settles from the NWS daily maximum",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ticker": f"{EVENT_TICKER}-{suffix}",
        "status": "active",
        "strike_type": strike_type,
        "rules_primary": rules,
        "expiration_time": "2026-08-15T04:00:00Z",
    }
    if floor is not None:
        payload["floor_strike"] = floor
    if cap is not None:
        payload["cap_strike"] = cap
    return payload


def partition(rules: str = "settles from the NWS daily maximum") -> list[dict[str, Any]]:
    """Four buckets that tile the integers, with both tails."""
    return [
        market("T98", strike_type="less", cap="99", rules=rules),
        market("B99", strike_type="between", floor="99", cap="100", rules=rules),
        market("B101", strike_type="between", floor="101", cap="102", rules=rules),
        market("T102", strike_type="greater", floor="102", rules=rules),
    ]


def event(markets: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return (
        {
            "event_ticker": EVENT_TICKER,
            "title": "Highest temperature in Testville",
            "mutually_exclusive": True,
        },
        markets,
    )


class TestDrafting:
    def test_a_clean_partition_is_drafted(self, session: Session) -> None:
        report = propose_from_events(session, [event(partition())])
        assert len(report.drafted) == 1
        assert report.drafted[0].legs == 4

    def test_a_draft_cannot_qualify_anything(self, session: Session) -> None:
        """The whole point. A proposal is a request for review, and an AI
        agent reading market structure may make one freely precisely because
        making one establishes nothing."""
        propose_from_events(session, [event(partition())])
        record = RelationshipRegistry(session).latest(slug_for(EVENT_TICKER))

        assert record is not None
        assert record.status is RelationshipStatus.PENDING
        assert record.status.may_qualify is False

    def test_the_draft_records_every_leg_terms_hash(self, session: Session) -> None:
        """An approval is bound to the wording it was given for, which is only
        possible if the draft captured that wording."""
        propose_from_events(session, [event(partition())])
        record = RelationshipRegistry(session).latest(slug_for(EVENT_TICKER))

        assert record is not None
        assert len(record.dependency_hashes) == 4
        assert all(len(h) == 64 for h in record.dependency_hashes.values())

    def test_the_draft_carries_its_own_case(self, session: Session) -> None:
        """A reviewer decides on evidence, not on this system's say-so. A
        proposal that only asserted "these four are exhaustive" would be asking
        for a rubber stamp."""
        propose_from_events(session, [event(partition())])
        record = RelationshipRegistry(session).latest(slug_for(EVENT_TICKER))

        assert record is not None
        proof = dict(record.payout_proof)
        assert proof["integer_coverage"]
        assert len(proof["boundaries"]) == 4
        assert proof["reviewer_must_confirm"]

    def test_legs_record_side_and_ratio_explicitly(self, session: Session) -> None:
        """Leaving "YES on every leg at equal size" to be inferred downstream
        is how a relationship silently comes to mean something else."""
        propose_from_events(session, [event(partition())])
        record = RelationshipRegistry(session).latest(slug_for(EVENT_TICKER))

        assert record is not None
        assert all(leg["side"] == "yes" and leg["ratio"] == 1 for leg in record.legs)


class TestRefusals:
    def test_a_set_with_a_gap_is_not_drafted(self, session: Session) -> None:
        """A set with a hole is not a basket, and drafting it would ask a
        reviewer to approve something this system already knows is broken."""
        holed = [
            market("T98", strike_type="less", cap="99"),
            market("B101", strike_type="between", floor="101", cap="102"),
            market("T102", strike_type="greater", floor="102"),
        ]
        report = propose_from_events(session, [event(holed)])

        assert report.drafted == []
        assert pending(session) == []

    def test_a_non_partition_is_not_drafted(self, session: Session) -> None:
        """Named candidates from an unbounded space are mutually exclusive and
        not collectively exhaustive -- the difference that decides whether a
        basket pays a dollar."""
        enumerated = [
            {"ticker": f"{EVENT_TICKER}-{name}", "status": "active", "strike_type": "custom"}
            for name in ("ALICE", "BOB", "CAROL")
        ]
        report = propose_from_events(session, [event(enumerated)])

        assert report.drafted == []
        assert all(o.action == "skipped" for o in report.outcomes)


class TestIdempotence:
    def test_reproposing_an_unchanged_event_drafts_nothing(self, session: Session) -> None:
        """A daily run would otherwise draft a new version of every partition
        every time, burying real changes under identical drafts and inviting
        approval by fatigue."""
        propose_from_events(session, [event(partition())])
        second = propose_from_events(session, [event(partition())])

        assert second.drafted == []
        assert second.outcomes[0].action == "unchanged"
        assert len(pending(session)) == 1

    def test_changed_terms_draft_a_new_version(self, session: Session) -> None:
        propose_from_events(session, [event(partition())])
        propose_from_events(session, [event(partition(rules="settles from a different source"))])

        record = RelationshipRegistry(session).latest(slug_for(EVENT_TICKER))
        assert record is not None
        assert record.version == 2


class TestSuspension:
    def test_changed_terms_suspend_an_existing_approval(self, session: Session) -> None:
        """FR-004, and the reason it runs on every pass rather than on a timer:
        the window between a terms change and someone noticing is exactly when
        a basket stops being a basket."""
        propose_from_events(session, [event(partition())])
        registry = RelationshipRegistry(session)
        approved = registry.latest(slug_for(EVENT_TICKER))
        assert approved is not None
        registry.approve(approved, reviewer="tester", evidence="read the settlement rules")

        report = propose_from_events(
            session, [event(partition(rules="settles from a different source"))]
        )

        assert len(report.suspended) == 1
        assert registry.latest(slug_for(EVENT_TICKER)) is not None
        assert approved.status.may_qualify is False, "a suspended relationship cannot qualify"

    def test_an_unchanged_event_does_not_disturb_an_approval(self, session: Session) -> None:
        propose_from_events(session, [event(partition())])
        registry = RelationshipRegistry(session)
        approved = registry.latest(slug_for(EVENT_TICKER))
        assert approved is not None
        registry.approve(approved, reviewer="tester", evidence="read the settlement rules")

        propose_from_events(session, [event(partition())])
        assert approved.status.may_qualify is True, "an untouched approval still qualifies"


class TestGroupedReview:
    """One reading applied to the set it genuinely covers.

    A proposal pass over live temperature markets drafts eighty-odd
    relationships, of which "Dallas high on the 14th" and "Dallas high on the
    15th" differ only in expiry and strike numbers. Asking someone to read
    eighty separate rule sets is how approval-by-fatigue happens, and a
    reviewer who has stopped reading is worse than none -- the record still
    says a person signed.
    """

    def test_the_same_claim_on_different_days_shares_a_fingerprint(self, session: Session) -> None:
        assert review_fingerprint(partition()) == review_fingerprint(
            [dict(m, ticker=m["ticker"].replace("AUG14", "AUG15")) for m in partition()]
        )

    def test_different_settlement_wording_does_not(self, session: Session) -> None:
        """The fingerprint is over what the reviewer reads. If the rules differ,
        one reading does not answer both."""
        assert review_fingerprint(partition()) != review_fingerprint(
            partition(rules="settles from a different source")
        )

    def test_approving_a_group_approves_each_one_separately(self, session: Session) -> None:
        """Not one approval standing in for many: each keeps its own record,
        its own reviewer, and its own binding to its own legs' terms."""
        propose_from_events(session, [event(partition())])
        other = [dict(m, ticker=m["ticker"].replace("HIGHTEST", "HIGHOTHER")) for m in partition()]
        session.flush()
        propose_from_events(
            session,
            [
                (
                    {"event_ticker": "KXHIGHOTHER-26AUG14", "mutually_exclusive": True},
                    other,
                )
            ],
        )

        first = RelationshipRegistry(session).latest(slug_for(EVENT_TICKER))
        assert first is not None
        approved = approve_group(
            session, fingerprint_of(first), reviewer="tester", evidence="read the NWS rules"
        )

        assert len(approved) == 2
        assert all(r.status is RelationshipStatus.APPROVED for r in approved)
        assert pending(session) == []

    def test_a_group_stops_at_the_fingerprint_boundary(self, session: Session) -> None:
        propose_from_events(session, [event(partition())])
        propose_from_events(
            session,
            [
                (
                    {"event_ticker": "KXHIGHOTHER-26AUG14", "mutually_exclusive": True},
                    [
                        dict(m, ticker=m["ticker"].replace("HIGHTEST", "HIGHOTHER"))
                        for m in partition(rules="a different settlement source entirely")
                    ],
                )
            ],
        )

        first = RelationshipRegistry(session).latest(slug_for(EVENT_TICKER))
        assert first is not None
        approved = approve_group(
            session, fingerprint_of(first), reviewer="tester", evidence="read the NWS rules"
        )

        assert len(approved) == 1
        assert len(pending(session)) == 1

    def test_an_empty_fingerprint_is_refused(self, session: Session) -> None:
        """It would otherwise sweep every draft that never recorded what a
        reviewer would have to read under a single signature."""
        with pytest.raises(RegistryError, match="empty review fingerprint"):
            approve_group(session, "", reviewer="tester", evidence="read something")

    def test_grouping_puts_the_same_claim_together(self, session: Session) -> None:
        propose_from_events(session, [event(partition())])
        propose_from_events(
            session,
            [
                (
                    {"event_ticker": "KXHIGHOTHER-26AUG14", "mutually_exclusive": True},
                    [
                        dict(m, ticker=m["ticker"].replace("HIGHTEST", "HIGHOTHER"))
                        for m in partition()
                    ],
                )
            ],
        )

        groups = group_pending(session)
        assert len(groups) == 1
        assert len(next(iter(groups.values()))) == 2


class TestReport:
    def test_the_report_says_nothing_is_approved(self, session: Session) -> None:
        rendered = propose_from_events(session, [event(partition())]).render()
        assert "Nothing above is approved" in rendered
        assert "cannot qualify any candidate" in rendered

    def test_the_report_tells_the_reader_how_to_approve(self, session: Session) -> None:
        rendered = propose_from_events(session, [event(partition())]).render()
        assert "--reviewer" in rendered
        assert "--evidence" in rendered
