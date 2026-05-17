"""Cross-Family Confluence Booster.

When multiple strategy families vote the same `(symbol, side)` within a short
rolling window, the booster amplifies the next signal in that direction. When
families disagree, the booster shrinks to a probe.

The implementation is a pure, in-memory ledger backed by a small public API:

    from analysis.confluence import ConfluenceLedger, ConfluenceBooster
    ledger = ConfluenceLedger(window_minutes=10)
    booster = ConfluenceBooster(ledger=ledger)
    ledger.record_vote(family="scalp_xauusd", symbol="XAUUSD", side="long")
    multiplier = booster.multiplier_for(symbol="XAUUSD", side="long")
"""
from __future__ import annotations

from analysis.confluence.booster import ConfluenceBooster, ConfluenceMultiplier
from analysis.confluence.ledger import ConfluenceLedger, Vote


__all__ = [
    "ConfluenceLedger",
    "ConfluenceBooster",
    "ConfluenceMultiplier",
    "Vote",
]
