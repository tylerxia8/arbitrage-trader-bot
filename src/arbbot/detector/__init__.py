"""Candidate detection. Proposes intents; only the risk engine authorises."""

from __future__ import annotations

from arbbot.detector.basket import BasketEvaluation, BasketRequest, LegQuote, evaluate_basket

__all__ = ["BasketEvaluation", "BasketRequest", "LegQuote", "evaluate_basket"]
