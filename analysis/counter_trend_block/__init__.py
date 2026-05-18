"""Counter-Trend Direction Blocker.

Lesson 2026-05-18 07:30 UTC (pid=621571277, -$70.23): the scalp lane fired
SHORT @ 4547.38 right into an +11.5pt rally that took out the SL in 43
seconds. The main XAU scanner rejects these with a microstructure check
(``positive_delta_bias`` vs proposed direction), but the scalp lane has
no such filter.

This module is a small, additive guard the scanner can call before
emitting a scalp signal. It looks at the last `N` M1 closes and:
  - When all `N` closes move with the trend AND average body magnitude is
    above `min_body_atr_ratio × ATR`, BLOCK the counter-trend direction.
  - Otherwise PASS — chop is allowed both ways.

The blocker NEVER touches the proven path when its feature flag is OFF;
when ON, it returns a `BlockDecision` and the caller chooses what to do
(skip dispatch, shadow-log, etc.).
"""
from __future__ import annotations

from analysis.counter_trend_block.blocker import (
    BlockDecision,
    CounterTrendBlocker,
    CounterTrendBlockerConfig,
    M1Candle,
)


__all__ = [
    "CounterTrendBlocker",
    "CounterTrendBlockerConfig",
    "BlockDecision",
    "M1Candle",
]
