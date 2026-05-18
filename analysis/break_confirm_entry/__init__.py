"""Break-Confirm Entry — convert chase LIMIT into wait-break STOP.

Lesson 2026-05-18 (-$70.23 pid=621571277 and -$33.12 pid=621437227):
Limit orders placed against a fresh impulse get SL-hunted. Operator
directive: "เข้าเร็วไป limit order ไม่เหมาะ โดนหลอก SL ตลอด แล้วถึงลง
ต้องรอให้เบรคก่อนลงก่อนแล้วค่อย live entry เลย เหมาะกว่า"
("Entering too early; limit orders are NOT appropriate. Wait for break
*then* live entry.")

This module transforms a counter-trend LIMIT scalp signal into a STOP
order sitting beyond a recent swing extreme. The order is dormant until
the broker sees price actually break in the signal's direction — at
which point the broker turns it into a market fill. We preserve the
trading idea but only act when the tape confirms.

Public surface:

    from analysis.break_confirm_entry import (
        BreakConfirmEntry, BreakConfirmEntryConfig, BreakConfirmInputs,
        BreakConfirmDecision,
    )

    decision = converter.evaluate(inputs)
    if decision.converted:
        signal.entry = decision.new_entry
        signal.entry_type = decision.new_entry_type
        signal.stop_loss = decision.new_stop_loss
        signal.take_profit = decision.new_take_profit
"""
from __future__ import annotations

from analysis.break_confirm_entry.converter import (
    BreakConfirmDecision,
    BreakConfirmEntry,
    BreakConfirmEntryConfig,
    BreakConfirmInputs,
)


__all__ = [
    "BreakConfirmEntry",
    "BreakConfirmEntryConfig",
    "BreakConfirmInputs",
    "BreakConfirmDecision",
]
