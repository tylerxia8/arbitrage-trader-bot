"""Proposing that two venues price the same claim.

The trade is trivial arithmetic and the risk is entirely in the pairing, so
almost every test here is about the pairing refusing to assert anything.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from arbbot.registry import RelationshipRegistry
from arbbot.registry.crossvenue import find_candidates, propose_pairs, similarity
from arbbot.relationships import RelationshipStatus, RelationshipType

RUN = {
    "ticker": "KX2028RRUN-28-RDES",
    "yes_sub_title": "Ron DeSantis",
    "event_title": "Who will run for the 2028 Republican presidential nomination?",
    "rules_primary": "Resolves Yes if Ron DeSantis files as a candidate.",
}
WIN = {
    "venue": "polymarket",
    "market_id": "991",
    "question": "Will Ron DeSantis win the 2028 Republican presidential nomination?",
    "rules": "Resolves Yes if Ron DeSantis is the nominee.",
}
SAME = {
    "venue": "polymarket",
    "market_id": "992",
    "question": "Will Ron DeSantis run for the 2028 Republican presidential nomination?",
    "rules": "Resolves Yes if Ron DeSantis files as a candidate.",
}


def kalshi(**over: Any) -> dict[str, Any]:
    return {**RUN, **over}


class TestSimilarityIsNotEvidence:
    def test_run_and_win_score_high_and_are_different_events(self) -> None:
        """The trap, stated as a test. These two questions are nearly identical
        as text and describe different events -- running is far likelier than
        winning, so the price gap is large and reads like an arbitrage."""
        score = similarity(f"{RUN['yes_sub_title']} {RUN['event_title']}", str(WIN["question"]))
        assert score > 0.7, "a token matcher cannot tell these apart"

    def test_an_empty_question_scores_zero(self) -> None:
        assert similarity("", "anything at all") == 0.0

    def test_the_score_is_recorded_as_ordering_not_proof(self, session: Session) -> None:
        propose_pairs(session, find_candidates([kalshi()], [WIN]))
        record = RelationshipRegistry(session).latest("xvenue:KX2028RRUN-28-RDES~polymarket:991")

        assert record is not None
        proof = dict(record.payout_proof)
        assert "establishes nothing" in proof["similarity_is_not_evidence"]
        assert "RUN FOR" in proof["similarity_is_not_evidence"]


class TestCandidates:
    def test_a_plausible_pair_is_found(self) -> None:
        found = find_candidates([kalshi()], [SAME])
        assert len(found) == 1
        assert found[0].other_id == "992"

    def test_an_unrelated_market_is_not(self) -> None:
        other = {"venue": "polymarket", "market_id": "1", "question": "Will it rain in Cairo?"}
        assert find_candidates([kalshi()], [other]) == []

    def test_market_level_titles_are_used(self) -> None:
        """Kalshi lists a nomination as one event with a market per candidate,
        while the other venue lists each candidate as its own question.
        Comparing event titles pairs a container against a contract."""
        found = find_candidates([kalshi()], [WIN, SAME])
        assert {c.other_id for c in found} == {"991", "992"}

    def test_a_market_with_no_title_is_skipped(self) -> None:
        blank = {"ticker": "X", "yes_sub_title": "", "event_title": ""}
        assert find_candidates([blank], [WIN]) == []


class TestProposals:
    def test_a_pair_is_drafted_pending(self, session: Session) -> None:
        drafted = propose_pairs(session, find_candidates([kalshi()], [SAME]))
        assert len(drafted) == 1

        record = RelationshipRegistry(session).latest(drafted[0])
        assert record is not None
        assert record.status is RelationshipStatus.PENDING
        assert record.status.may_qualify is False

    def test_the_legs_are_opposite_sides_on_different_venues(self, session: Session) -> None:
        """YES on one and NO on the other. Both YES would be a doubled bet, not
        a hedge."""
        drafted = propose_pairs(session, find_candidates([kalshi()], [SAME]))
        record = RelationshipRegistry(session).latest(drafted[0])

        assert record is not None
        sides = {leg["venue"]: leg["side"] for leg in record.legs}
        assert sides == {"kalshi": "yes", "polymarket": "no"}

    def test_both_settlement_texts_are_carried_verbatim(self, session: Session) -> None:
        """The reviewer is being asked whether these resolve identically in
        every state, and cannot answer it from anything less than the words."""
        drafted = propose_pairs(session, find_candidates([kalshi()], [WIN]))
        proof = dict(RelationshipRegistry(session).latest(drafted[0]).payout_proof)  # type: ignore[union-attr]

        assert proof["kalshi_settlement"] == RUN["rules_primary"]
        assert proof["other_settlement"] == WIN["rules"]
        assert proof["kalshi_question"]
        assert proof["other_question"]

    def test_the_reviewer_is_asked_about_divergence_not_subject(self, session: Session) -> None:
        drafted = propose_pairs(session, find_candidates([kalshi()], [WIN]))
        confirms = dict(
            RelationshipRegistry(session).latest(drafted[0]).payout_proof  # type: ignore[union-attr]
        )["reviewer_must_confirm"]

        joined = " ".join(confirms)
        assert "EVERY state" in joined
        assert "same source" in joined
        assert "same time" in joined
        assert "void or refund" in joined

    def test_both_venues_terms_are_hashed(self, session: Session) -> None:
        """An approval is bound to the wording it was given for on *both*
        sides, so a rewritten description on either suspends the pair."""
        drafted = propose_pairs(session, find_candidates([kalshi()], [SAME]))
        record = RelationshipRegistry(session).latest(drafted[0])

        assert record is not None
        assert len(record.dependency_hashes) == 2
        assert all(len(h) == 64 for h in record.dependency_hashes.values())

    def test_the_relationship_type_says_what_it_is(self, session: Session) -> None:
        drafted = propose_pairs(session, find_candidates([kalshi()], [SAME]))
        record = RelationshipRegistry(session).latest(drafted[0])

        assert record is not None
        assert record.relationship_type is RelationshipType.CROSS_VENUE_PAIR

    def test_reproposing_the_same_pair_drafts_nothing(self, session: Session) -> None:
        found = find_candidates([kalshi()], [SAME])
        propose_pairs(session, found)
        assert propose_pairs(session, found) == []
