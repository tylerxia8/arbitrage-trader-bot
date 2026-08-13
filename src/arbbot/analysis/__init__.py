"""Read-only analysis over the collected archive.

Nothing here qualifies a candidate or approves a relationship. These are
research reads that answer whether a collection run is producing anything
worth the milestones that follow.
"""

from __future__ import annotations

from arbbot.analysis.baskets import BasketObservation, ScanResult, scan_baskets
from arbbot.analysis.falsification import FalsificationReport, run_falsification

__all__ = [
    "BasketObservation",
    "FalsificationReport",
    "ScanResult",
    "run_falsification",
    "scan_baskets",
]
