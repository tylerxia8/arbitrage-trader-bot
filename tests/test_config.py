"""Configuration validation.

These tests exist to prove the system fails closed. Each one describes a
misconfiguration that a real deployment could plausibly produce, and asserts
that it stops startup rather than producing a subtly wrong running system.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from arbbot import buildflags
from arbbot.config import Environment, RiskLimits, Settings


def make_settings(**overrides: object) -> Settings:
    """Build settings without reading the developer's .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


class TestDefaults:
    def test_defaults_are_inert(self) -> None:
        settings = make_settings()
        assert settings.environment is Environment.LOCAL
        assert settings.live_trading_enabled is False
        assert settings.may_submit_live_orders() is False

    def test_build_ships_disarmed(self) -> None:
        """Milestone 0 must not contain an armed execution path at all."""
        assert buildflags.LIVE_EXECUTION_COMPILED_IN is False
        assert buildflags.DEMO_EXECUTION_COMPILED_IN is False

    def test_default_risk_limits_match_specification(self) -> None:
        limits = RiskLimits(_env_file=None)
        assert limits.max_order_notional_usd == Decimal("5")
        assert limits.max_unmatched_exposure_usd == Decimal("25")
        assert limits.max_total_open_exposure_usd == Decimal("100")
        assert limits.daily_loss_limit_usd == Decimal("10")


class TestLiveGates:
    def test_runtime_flag_alone_cannot_arm_the_system(self) -> None:
        """The whole point of FR-016: one variable is never enough."""
        with pytest.raises(ValidationError, match="no live-execution path"):
            make_settings(
                live_trading_enabled=True,
                environment=Environment.LIVE_SUPERVISED,
                venue_api_key_id="k",
                venue_private_key_pem="pem",
            )

    def test_live_flag_in_a_research_environment_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            make_settings(live_trading_enabled=True, environment=Environment.RESEARCH)

    def test_may_submit_requires_all_standing_gates(self) -> None:
        settings = make_settings()
        assert settings.may_submit_live_orders() is False


class TestCredentialScoping:
    def test_research_environment_refuses_a_credential(self) -> None:
        """Research collection uses public endpoints; a key present there is a
        sign that live keys have leaked into the wrong deployment."""
        with pytest.raises(ValidationError, match="public endpoints only"):
            make_settings(
                environment=Environment.RESEARCH,
                venue_api_key_id="leaked",
                venue_private_key_pem="pem",
            )

    def test_demo_environment_requires_both_credential_halves(self) -> None:
        with pytest.raises(ValidationError, match="requires both"):
            make_settings(environment=Environment.DEMO, venue_api_key_id="k")

    def test_demo_environment_accepts_a_complete_credential(self) -> None:
        settings = make_settings(
            environment=Environment.DEMO,
            venue_api_key_id="k",
            venue_private_key_pem="pem",
        )
        assert settings.environment is Environment.DEMO

    def test_secrets_are_not_rendered(self) -> None:
        settings = make_settings(
            environment=Environment.DEMO,
            venue_api_key_id="super-secret-id",
            venue_private_key_pem="super-secret-pem",
        )
        assert "super-secret-id" not in repr(settings)
        assert "super-secret-pem" not in str(settings)


class TestRiskLimitValidation:
    def test_zero_limit_is_rejected(self) -> None:
        """A zero cap reads as 'unlimited' to a careless reviewer; refuse it."""
        with pytest.raises(ValidationError, match="must be positive"):
            RiskLimits(_env_file=None, max_order_notional_usd=Decimal("0"))

    def test_per_leg_cap_above_aggregate_cap_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="could never be placed"):
            RiskLimits(
                _env_file=None,
                max_order_notional_usd=Decimal("500"),
                max_total_open_exposure_usd=Decimal("100"),
            )

    def test_unmatched_cap_above_aggregate_cap_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="would never bind"):
            RiskLimits(
                _env_file=None,
                max_unmatched_exposure_usd=Decimal("500"),
                max_total_open_exposure_usd=Decimal("100"),
            )

    def test_absurd_staleness_threshold_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not evidence"):
            RiskLimits(_env_file=None, max_quote_age_ms=120_000)


class TestMiscValidation:
    def test_unknown_log_level_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="log_level must be"):
            make_settings(log_level="CHATTY")

    def test_log_level_is_normalised(self) -> None:
        assert make_settings(log_level="debug").log_level == "DEBUG"

    def test_non_postgres_database_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be PostgreSQL"):
            make_settings(database_url="mysql://localhost/arbbot")

    def test_unknown_setting_is_rejected(self) -> None:
        """extra='forbid' catches typos that would otherwise be ignored."""
        with pytest.raises(ValidationError):
            make_settings(live_tradng_enabled=True)
