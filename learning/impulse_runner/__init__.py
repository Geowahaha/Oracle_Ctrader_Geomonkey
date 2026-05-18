"""Impulse Runner — TP extender + SL trailer for impulse-leg captures.

Lesson learned 2026-05-18 (XAU 05:24-07:30 UTC):
The scalp lane caught one piece (+$44) of a 60pt impulse that played out
right after the trade closed. The fixed TP at ~5pt is correct for chop,
catastrophic during impulse. This module bolts a runner mode onto live
positions when *both* are true:

  1. MFE has crossed `min_mfe_r` (the trade is already winning).
  2. Structure_break is confirmed in the trade's direction (the impulse
     leg is real, not a retracement spike).

When both hold, we issue a `TrailingDirective`:
  - Extend take-profit to a chandelier-style target derived from ATR.
  - Trail stop-loss to lock partial profit while preserving runner.

This is *additive*: it does not modify the entry, the original SL/TP, or
any proven strategy. It only fires on positions that already passed
all entry gates and that prove themselves with real MFE.
"""
from __future__ import annotations

from learning.impulse_runner.engine import (
    ImpulseRunner,
    ImpulseRunnerConfig,
    PositionSnapshot,
    StructureBreakSignal,
    TrailingDirective,
)


__all__ = [
    "ImpulseRunner",
    "ImpulseRunnerConfig",
    "PositionSnapshot",
    "StructureBreakSignal",
    "TrailingDirective",
]
