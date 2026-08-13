"""Candidate detection. Proposes intents; only the risk engine authorises."""

from __future__ import annotations

from arbbot.detector.basket import BasketEvaluation, BasketRequest, LegQuote, evaluate_basket
from arbbot.detector.implication import ImplicationRequest, evaluate_implication, minimum_payout

__all__ = [
    "BasketEvaluation",
    "BasketRequest",
    "ImplicationRequest",
    "LegQuote",
    "evaluate_basket",
    "evaluate_implication",
    "minimum_payout",
]
