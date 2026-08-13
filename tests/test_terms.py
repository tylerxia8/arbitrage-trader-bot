"""Settlement-terms normalization and hashing (EPIC-5, FR-004).

The hash is a judgement about what counts as material, so these tests are
written as two opposing failure modes: does it stay quiet through a change
that matters, and does it cry wolf at one that does not. The first costs
money silently; the second makes the alarm worthless.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arbbot.normalize import MATERIAL_FIELDS, normalize_kalshi_market

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"


def market(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticker": "KXHIGHTATL-26AUG13-T92",
        "event_ticker": "KXHIGHTATL-26AUG13",
        "strike_type": "between",
        "floor_strike": "92",
        "cap_strike": "93",
        "rules_primary": "If the maximum temperature at Atlanta is between 92-93 degrees...",
        "expiration_time": "2026-08-30T00:20:00Z",
        "settlement_timer_seconds": 5,
        # Volatile fields the hash must ignore.
        "yes_bid_dollars": "0.4200",
        "volume_fp": "1234.00",
        "last_price_dollars": "0.4300",
    }
    return base | overrides


class TestStability:
    def test_the_same_terms_hash_the_same(self) -> None:
        assert normalize_kalshi_market(market()).terms_hash == (
            normalize_kalshi_market(market()).terms_hash
        )

    def test_price_movement_does_not_change_the_hash(self) -> None:
        """The cry-wolf failure. Folding price into the hash would suspend
        every relationship on every tick, and an alarm that fires
        continuously is one nobody reads."""
        quiet = normalize_kalshi_market(market())
        busy = normalize_kalshi_market(
            market(yes_bid_dollars="0.9900", volume_fp="99999.00", last_price_dollars="0.98")
        )
        assert quiet.terms_hash == busy.terms_hash

    def test_reflowed_rule_text_does_not_change_the_hash(self) -> None:
        """The venue reflows its own prose; a moved line break is not a
        settlement change."""
        original = normalize_kalshi_market(market())
        reflowed = normalize_kalshi_market(
            market(
                rules_primary=(
                    "If the maximum temperature at Atlanta   is between\n92-93 degrees..."
                )
            )
        )
        assert original.terms_hash == reflowed.terms_hash

    def test_unknown_new_venue_fields_do_not_change_the_hash(self) -> None:
        """The field list is closed, so a venue adding a column cannot
        silently invalidate every approval in the registry."""
        original = normalize_kalshi_market(market())
        extended = normalize_kalshi_market(market(some_new_venue_field="whatever"))
        assert original.terms_hash == extended.terms_hash


class TestSensitivity:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("floor_strike", "91"),
            ("cap_strike", "94"),
            ("strike_type", "greater"),
            ("rules_primary", "If the maximum temperature at Boston is between 92-93..."),
            ("expiration_time", "2026-09-30T00:20:00Z"),
            ("settlement_timer_seconds", 600),
        ],
    )
    def test_material_changes_move_the_hash(self, field: str, value: Any) -> None:
        """The silent failure this exists to prevent. A bucket's bounds
        shifting by a degree can stop a basket being exhaustive while every
        price looks normal."""
        before = normalize_kalshi_market(market())
        after = normalize_kalshi_market(market(**{field: value}))
        assert before.terms_hash != after.terms_hash

    def test_the_changed_field_is_named(self) -> None:
        """A suspension saying only "terms changed" sends someone to diff two
        walls of legal text."""
        before = normalize_kalshi_market(market())
        after = normalize_kalshi_market(market(cap_strike="94"))
        assert before.differs_from(after) == ("cap_strike",)

    def test_multiple_changes_are_all_named(self) -> None:
        before = normalize_kalshi_market(market())
        after = normalize_kalshi_market(market(floor_strike="90", cap_strike="94"))
        assert set(before.differs_from(after)) == {"floor_strike", "cap_strike"}


class TestContract:
    def test_a_market_without_a_ticker_is_refused(self) -> None:
        """A hash that cannot say which contract it describes is not
        evidence of anything."""
        with pytest.raises(ValueError, match="no ticker"):
            normalize_kalshi_market({"rules_primary": "..."})

    def test_the_parser_version_is_recorded(self) -> None:
        assert normalize_kalshi_market(market()).parser_version == "terms-v1"

    def test_every_material_field_is_captured(self) -> None:
        normalized = normalize_kalshi_market(market())
        assert set(normalized.fields) == set(MATERIAL_FIELDS)


class TestRealMarket:
    def test_the_captured_market_normalizes(self) -> None:
        """Against the live payload captured on 2026-08-12."""
        payload = json.loads((FIXTURES / "market_rest.json").read_text(encoding="utf-8"))
        normalized = normalize_kalshi_market(payload["market"])

        assert normalized.ticker == "KXNFLGAME-26AUG15DALSEA-SEA"
        assert len(normalized.terms_hash) == 64

    def test_the_captured_market_is_stable_under_price_change(self) -> None:
        payload = json.loads((FIXTURES / "market_rest.json").read_text(encoding="utf-8"))
        original = normalize_kalshi_market(payload["market"])
        moved = normalize_kalshi_market(
            payload["market"] | {"yes_bid_dollars": "0.0100", "volume_fp": "1.00"}
        )
        assert original.terms_hash == moved.terms_hash
