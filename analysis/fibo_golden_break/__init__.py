"""Fibo Golden-Break Entry — wait for actual break-bar inside 0.50-0.88 zone.

Operator directive 2026-05-18: "ตรวจ fibo family ตอนนี้ ... รอแท่งเบรคจริง
ใน golden ratio 0.50 <-> 0.618 <-> 0.88 ก่อน entry จริง" — fibo signals
should not chase the retracement with a LIMIT order. Wait for an actual
break-bar inside the golden zone, then live entry.

The legacy Sniper/Scout paths in scanners/fibo_advance.py emit LIMIT
orders at the golden pocket price. That fills passively as price drifts
through the zone — same chase pattern that produced the -$70 scalp loss.

This module evaluates the signal alongside fresh M1 candle data:

  - If price is inside the golden zone AND a break-bar in the trade's
    direction has already printed (M1 close beyond recent micro swing)
    → upgrade to LIVE MARKET (the move is happening now).
  - If price is inside the golden zone but no break yet
    → convert to a STOP order at the recent micro swing + small buffer
      (broker triggers when price actually breaks).
  - If price is OUTSIDE the golden zone
    → skip (signal too early or retracement too deep — operator can
      wait for a fresh signal at the proper zone).

Public surface:

    from analysis.fibo_golden_break import (
        FiboGoldenBreakEntry, FiboGoldenBreakConfig, FiboGoldenInputs,
        FiboGoldenDecision, ACTION_LIVE_MARKET, ACTION_WAIT_BREAK_STOP, ACTION_SKIP,
    )
"""
from __future__ import annotations

from analysis.fibo_golden_break.entry import (
    ACTION_LIVE_MARKET,
    ACTION_SKIP,
    ACTION_WAIT_BREAK_STOP,
    FiboGoldenBreakConfig,
    FiboGoldenBreakEntry,
    FiboGoldenDecision,
    FiboGoldenInputs,
)


__all__ = [
    "FiboGoldenBreakEntry",
    "FiboGoldenBreakConfig",
    "FiboGoldenInputs",
    "FiboGoldenDecision",
    "ACTION_LIVE_MARKET",
    "ACTION_WAIT_BREAK_STOP",
    "ACTION_SKIP",
]
