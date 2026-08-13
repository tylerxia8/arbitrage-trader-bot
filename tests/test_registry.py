"""The relationship registry (FR-004, FR-005).

The registry's whole job is to keep one distinction sharp: a claim someone
inferred versus a claim someone signed for. These tests attack that boundary
from both sides -- can an unapproved relationship qualify anything, and can an
approval outlive the terms it was given for.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from arbbot.db.models import RelationshipRecord
from arbbot.reasons import RejectionReason
from arbbot.registry import RegistryError, RelationshipRegistry
from arbbot.relationships import RelationshipStatus, RelationshipType

LEGS = [{"ticker": "KXHIGHTATL-T86", "side": "yes"}, {"ticker": "KXHIGHTATL-T93", "side": "yes"}]
TERMS = {"KXHIGHTATL-T86": "hash-a", "KXHIGHTATL-T93": "hash-b"}
PROOF = {"states": [["yes", "no"], ["no", "yes"]], "min_payout": "1.00"}


def draft(registry: RelationshipRegistry, **kw: object) -> RelationshipRecord:
    return registry.draft(
        slug=kw.get("slug", "atl-high"),  # type: ignore[arg-type]
        relationship_type=RelationshipType.INTERVAL_PARTITION,
        legs=kw.get("legs", LEGS),  # type: ignore[arg-type]
        payout_proof=PROOF,
        dependency_hashes=kw.get("terms", TERMS),  # type: ignore[arg-type]
    )


class TestDrafting:
    def test_a_draft_starts_pending(self, session: Session) -> None:
        record = draft(RelationshipRegistry(session))
        assert record.status is RelationshipStatus.PENDING

    def test_a_pending_draft_cannot_qualify_anything(self, session: Session) -> None:
        """FR-005. Anything may draft -- including an AI agent reading market
        structure -- precisely because a draft is inert."""
        registry = RelationshipRegistry(session)
        check = registry.check_usable(draft(registry), TERMS)

        assert not check.usable
        assert check.reason is RejectionReason.RELATIONSHIP_NOT_APPROVED

    def test_versions_increment_per_slug(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        assert draft(registry).version == 1
        assert draft(registry).version == 2

    def test_a_draft_must_record_its_legs_terms(self, session: Session) -> None:
        """Without them an approval cannot be bound to the wording it was
        given for, which is the entire mechanism of FR-004."""
        registry = RelationshipRegistry(session)
        with pytest.raises(RegistryError, match="terms hash"):
            registry.draft(
                slug="x",
                relationship_type=RelationshipType.EXHAUSTIVE_BASKET,
                legs=LEGS,
                payout_proof=PROOF,
                dependency_hashes={},
            )

    def test_a_single_leg_is_not_a_relationship(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        with pytest.raises(RegistryError, match="at least two legs"):
            registry.draft(
                slug="x",
                relationship_type=RelationshipType.EXHAUSTIVE_BASKET,
                legs=[LEGS[0]],
                payout_proof=PROOF,
                dependency_hashes=TERMS,
            )


class TestApproval:
    def test_approval_makes_a_relationship_usable(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        record = draft(registry)
        registry.approve(record, reviewer="tyler", evidence="read NWS settlement rules")

        assert record.status is RelationshipStatus.APPROVED
        assert registry.check_usable(record, TERMS).usable

    def test_an_approval_must_name_a_reviewer(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        with pytest.raises(RegistryError, match="reviewer"):
            registry.approve(draft(registry), reviewer="  ", evidence="x")

    def test_an_approval_must_record_what_was_read(self, session: Session) -> None:
        """An approval that cannot say what was confirmed is a rubber stamp
        with a name on it."""
        registry = RelationshipRegistry(session)
        with pytest.raises(RegistryError, match="evidence"):
            registry.approve(draft(registry), reviewer="tyler", evidence="")

    def test_the_approval_record_is_kept(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        record = draft(registry)
        approval = registry.approve(
            record, reviewer="tyler", evidence="NWS CLI report, whole degrees"
        )
        assert approval.reviewer == "tyler"
        assert "whole degrees" in approval.evidence

    def test_a_rejected_relationship_is_retired(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        record = draft(registry)
        registry.reject(record, reviewer="tyler", evidence="buckets do not tile")

        assert record.status is RelationshipStatus.RETIRED
        assert not registry.check_usable(record, TERMS).usable

    def test_a_retired_relationship_cannot_be_approved(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        record = draft(registry)
        registry.reject(record, reviewer="tyler", evidence="no")
        with pytest.raises(RegistryError, match="retired"):
            registry.approve(record, reviewer="tyler", evidence="changed my mind")


class TestTermsBinding:
    def test_changed_terms_refuse_the_relationship(self, session: Session) -> None:
        """FR-004. A basket that was exhaustive under the old wording may not
        be under the new one."""
        registry = RelationshipRegistry(session)
        record = draft(registry)
        registry.approve(record, reviewer="tyler", evidence="read the rules")

        moved = {**TERMS, "KXHIGHTATL-T86": "hash-changed"}
        check = registry.check_usable(record, moved)

        assert not check.usable
        assert check.reason is RejectionReason.TERMS_CHANGED

    def test_a_missing_leg_refuses_the_relationship(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        record = draft(registry)
        registry.approve(record, reviewer="tyler", evidence="read the rules")

        check = registry.check_usable(record, {"KXHIGHTATL-T86": "hash-a"})
        assert not check.usable
        assert check.reason is RejectionReason.MARKET_NOT_OPEN

    def test_an_extra_leg_refuses_the_relationship(self, session: Session) -> None:
        """A leg the reviewer never saw. The set they signed for is not the
        set in front of us, and a basket missing an outcome pays nothing when
        that outcome occurs."""
        registry = RelationshipRegistry(session)
        record = draft(registry)
        registry.approve(record, reviewer="tyler", evidence="read the rules")

        check = registry.check_usable(record, {**TERMS, "KXHIGHTATL-NEW": "hash-c"})
        assert not check.usable
        assert check.reason is RejectionReason.TERMS_CHANGED

    def test_suspension_withdraws_a_relationship(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        record = draft(registry)
        registry.approve(record, reviewer="tyler", evidence="read the rules")
        registry.suspend(record, why="terms hash moved")

        assert record.status is RelationshipStatus.SUSPENDED
        assert not registry.check_usable(record, TERMS).usable


class TestQueries:
    def test_only_approved_relationships_are_listed(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        approved = draft(registry, slug="approved")
        registry.approve(approved, reviewer="tyler", evidence="read")
        draft(registry, slug="pending")

        slugs = {r.slug for r in registry.approved()}
        assert slugs == {"approved"}

    def test_latest_returns_the_newest_version(self, session: Session) -> None:
        registry = RelationshipRegistry(session)
        draft(registry, slug="s")
        draft(registry, slug="s")
        latest = registry.latest("s")
        assert latest is not None
        assert latest.version == 2
