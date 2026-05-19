"""Breathing Room — block premature close_position directives.

Operator complaint 2026-05-18: "ระบบไม่หิวกำไร แต่ป้องกันการสูญเสีย
อย่างเดียว" — the system kills trades too early on both sides.

Today's 16 XAU trades net -$154.46:
- 12 losers with avg -$21 (many at only 0.5-2pt against entry)
- 4 winners with avg +$25 (one captured only 6% of 2R MFE)

Pattern: the XAU_PROFIT_GUARDIAN issues ``close_position`` directives on
"weak" positions in T1_PRUNE/T2_CRYSTALLIZE/T3_HARVEST states. The
weakness_score uses MFE, so a brand-new position with no time to build
MFE looks weak and gets pruned — locking 0.5-1.5pt as a $5-$15 loss
before the trade has any chance to find direction.

This filter wraps the guardian's directive list and:
  1. **Blocks** close_position for positions younger than
     ``min_breathing_minutes`` (default 10) UNLESS the original SL/TP
     was actually hit by price.
  2. **Blocks** close_position when MFE_r is still positive (the trade
     is winning, give it room).
  3. **Blocks** close_position when MAE_r < ``allow_mae_r`` (the trade
     hasn't decisively moved against us yet).
  4. Leaves all *protective* actions intact (partial_close,
     hold_position, cancel_order, etc.).

Public surface:

    from execution.breathing_room import (
        BreathingRoomFilter, BreathingRoomConfig, PositionRoomState,
        FilterDecision,
    )
"""
from __future__ import annotations

from execution.breathing_room.filter import (
    BreathingRoomConfig,
    BreathingRoomFilter,
    FilterDecision,
    PositionRoomState,
)


__all__ = [
    "BreathingRoomFilter",
    "BreathingRoomConfig",
    "FilterDecision",
    "PositionRoomState",
]
