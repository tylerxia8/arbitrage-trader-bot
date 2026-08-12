"""Validated application configuration.

Configuration is parsed once at startup and fails loudly. A misconfigured
trading system that starts anyway is more dangerous than one that refuses to
boot, so every constraint here raises rather than falling back to a default.

Risk limits carry the initial values from the specification (section 22). They
are intentionally small; raising them is an owner decision backed by evidence,
not a convenience edit.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import Final

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from arbbot import buildflags
from arbbot.money import ZERO

__all__ = ["Environment", "RiskLimits", "Settings", "load_settings"]


class Environment(enum.StrEnum):
    """Deployment environment. Determines which credentials may be loaded."""

    LOCAL = "local"
    """Development and fixtures. Synthetic data; no venue credential."""

    RESEARCH = "research"
    """Public collection and replay. Public endpoints; persistent database."""

    DEMO = "demo"
    """Order-integration testing against mock funds."""

    LIVE_SUPERVISED = "live_supervised"
    """Tiny, manually approved live trading. Restricted keys, capped balance."""

    LIVE_BOUNDED = "live_bounded"
    """Later autonomous whitelist. Requires a separate release and sign-off."""

    @property
    def is_live(self) -> bool:
        return self in (Environment.LIVE_SUPERVISED, Environment.LIVE_BOUNDED)


#: Environments in which a venue credential is meaningful at all.
_CREDENTIALED: Final = (Environment.DEMO, Environment.LIVE_SUPERVISED, Environment.LIVE_BOUNDED)


class RiskLimits(BaseSettings):
    """Hard monetary limits. Every value is a rejection threshold, not a target."""

    model_config = SettingsConfigDict(env_prefix="ARBBOT_RISK_", extra="forbid")

    max_order_notional_usd: Decimal = Field(
        default=Decimal("5"),
        description="Per-leg cap. Intents above this are rejected, never truncated.",
    )
    max_unmatched_exposure_usd: Decimal = Field(
        default=Decimal("25"),
        description="Directional exposure from partial fills. Breach stops the strategy.",
    )
    max_total_open_exposure_usd: Decimal = Field(
        default=Decimal("100"),
        description="Aggregate open exposure. Breach rejects new intents.",
    )
    daily_loss_limit_usd: Decimal = Field(
        default=Decimal("10"),
        description="Realised plus mark-to-exit. Breach kills trading for the day.",
    )
    min_net_edge_usd: Decimal = Field(
        default=Decimal("0.02"),
        description="A candidate must clear this after every cost to qualify.",
    )
    max_quote_age_ms: int = Field(
        default=2_000,
        description="Books older than this are stale and cannot support an evaluation.",
    )

    @field_validator(
        "max_order_notional_usd",
        "max_unmatched_exposure_usd",
        "max_total_open_exposure_usd",
        "daily_loss_limit_usd",
        "min_net_edge_usd",
    )
    @classmethod
    def _must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= ZERO:
            raise ValueError("risk limits must be positive; a zero limit disables the control")
        return v

    @field_validator("max_quote_age_ms")
    @classmethod
    def _staleness_must_be_bounded(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_quote_age_ms must be positive")
        if v > 60_000:
            raise ValueError(
                "max_quote_age_ms above 60s cannot be justified: a minute-old book "
                "is not evidence of an executable price"
            )
        return v

    @model_validator(mode="after")
    def _limits_must_be_ordered(self) -> RiskLimits:
        if self.max_order_notional_usd > self.max_total_open_exposure_usd:
            raise ValueError(
                "max_order_notional_usd exceeds max_total_open_exposure_usd: "
                "a single leg could never be placed without breaching the aggregate cap"
            )
        if self.max_unmatched_exposure_usd > self.max_total_open_exposure_usd:
            raise ValueError(
                "max_unmatched_exposure_usd exceeds max_total_open_exposure_usd: "
                "the unmatched control would never bind"
            )
        return self


class Settings(BaseSettings):
    """Top-level settings. Construct via :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="ARBBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        secrets_dir=None,
    )

    environment: Environment = Environment.LOCAL
    database_url: str = "postgresql+psycopg://arbbot:arbbot@localhost:5432/arbbot"
    log_level: str = "INFO"

    venue_api_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    venue_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    venue_api_key_id: SecretStr | None = None
    venue_private_key_pem: SecretStr | None = None

    live_trading_enabled: bool = Field(
        default=False,
        description=(
            "Runtime half of the FR-016 gate. Setting this alone does nothing: "
            "the build flag and per-basket approval are still required."
        ),
    )

    risk: RiskLimits = Field(default_factory=RiskLimits)

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @field_validator("database_url")
    @classmethod
    def _database_url_is_postgres(cls, v: str) -> str:
        if not v.startswith(("postgresql+psycopg://", "sqlite://")):
            raise ValueError("database_url must be PostgreSQL (or SQLite for local fixtures only)")
        return v

    @model_validator(mode="after")
    def _credentials_match_environment(self) -> Settings:
        has_credential = self.venue_api_key_id is not None or self.venue_private_key_pem is not None

        if has_credential and self.environment not in _CREDENTIALED:
            raise ValueError(
                f"a venue credential is present but environment is {self.environment.value!r}; "
                "research and local runs must use public endpoints only"
            )
        if self.environment in _CREDENTIALED and not (
            self.venue_api_key_id and self.venue_private_key_pem
        ):
            raise ValueError(
                f"environment {self.environment.value!r} requires both "
                "ARBBOT_VENUE_API_KEY_ID and ARBBOT_VENUE_PRIVATE_KEY_PEM"
            )
        return self

    @model_validator(mode="after")
    def _live_flags_are_coherent(self) -> Settings:
        """Refuse configurations that *look* armed but are not, and vice versa.

        A deployment that sets ``live_trading_enabled`` on a build without the
        live path compiled in is a misunderstanding about what is running.
        Failing at startup surfaces that immediately rather than at the moment
        an order is expected to fire and silently is not.
        """
        if self.live_trading_enabled and not buildflags.LIVE_EXECUTION_COMPILED_IN:
            raise ValueError(
                "live_trading_enabled is set but this build has no live-execution path "
                "(buildflags.LIVE_EXECUTION_COMPILED_IN is False). Deploy a Milestone 5 "
                "build or unset the flag."
            )
        if self.live_trading_enabled and not self.environment.is_live:
            raise ValueError(
                f"live_trading_enabled is set in environment {self.environment.value!r}; "
                "live trading requires a live_supervised or live_bounded deployment"
            )
        return self

    def may_submit_live_orders(self) -> bool:
        """Return whether *both* standing gates of FR-016 are open.

        This is necessary, never sufficient. A caller that gets ``True`` here
        must still obtain an unexpired, in-bounds human approval for the
        specific basket before an order may be submitted.
        """
        return (
            buildflags.LIVE_EXECUTION_COMPILED_IN
            and self.live_trading_enabled
            and self.environment.is_live
        )


def load_settings() -> Settings:
    """Load and validate settings, raising on any inconsistency."""
    return Settings()
