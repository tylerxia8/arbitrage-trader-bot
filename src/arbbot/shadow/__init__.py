"""Shadow execution: what acquiring a basket would actually have cost."""

from __future__ import annotations

from arbbot.shadow.executor import (
    FillOutcome,
    LegFill,
    ShadowConfig,
    ShadowResult,
    simulate_basket,
)

__all__ = [
    "FillOutcome",
    "LegFill",
    "ShadowConfig",
    "ShadowResult",
    "simulate_basket",
]
