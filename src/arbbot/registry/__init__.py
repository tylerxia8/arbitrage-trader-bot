"""The relationship registry. Drafting is not approving (FR-005)."""

from __future__ import annotations

from arbbot.registry.proposal import (
    ProposalOutcome,
    ProposalReport,
    approve_group,
    fingerprint_of,
    group_pending,
    pending,
    propose_from_events,
    review_fingerprint,
    review_templates,
    rules_template,
    slug_for,
)
from arbbot.registry.service import RegistryError, RelationshipRegistry, UsabilityCheck

__all__ = [
    "ProposalOutcome",
    "ProposalReport",
    "RegistryError",
    "RelationshipRegistry",
    "UsabilityCheck",
    "approve_group",
    "fingerprint_of",
    "group_pending",
    "pending",
    "propose_from_events",
    "review_fingerprint",
    "review_templates",
    "rules_template",
    "slug_for",
]
