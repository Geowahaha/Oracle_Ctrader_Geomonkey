"""Anti Stop-Hunt SL Widener.

Lesson learned 2026-05-18: two of three scalp trades got stopped out at
~0.7pt and ~3pt above the swing high before price collapsed 60+ points.
Classic stop-hunt pattern: liquidity sits just beyond the obvious swing,
the market sweeps it, then continues in the original direction.

This module wraps the proposed SL with a *floor* that pushes it beyond
the recent swing extreme plus an ATR-scaled buffer. It is purely additive
— callers pass in their existing SL and the widener returns the safer of
the two. When the engine is disabled or inputs are unsafe, it returns the
original SL unchanged.

Public surface:

    from analysis.anti_stop_hunt import widen_sl_for_anti_hunt, AntiHuntConfig
    safe_sl = widen_sl_for_anti_hunt(
        entry=2300.0, direction="short", proposed_sl=2302.0,
        swing_extreme=2301.50, atr=2.0, config=AntiHuntConfig(enabled=True),
    )
"""
from __future__ import annotations

from analysis.anti_stop_hunt.widener import (
    AntiHuntConfig,
    AntiHuntDecision,
    widen_sl_for_anti_hunt,
)


__all__ = ["AntiHuntConfig", "AntiHuntDecision", "widen_sl_for_anti_hunt"]
