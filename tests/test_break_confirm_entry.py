"""Tests for the Break-Confirm Entry converter."""
from __future__ import annotations

from analysis.break_confirm_entry import (
    BreakConfirmDecision,
    BreakConfirmEntry,
    BreakConfirmEntryConfig,
    BreakConfirmInputs,
)


def _short_chase_inputs() -> BreakConfirmInputs:
    # Mirror 2026-05-18 07:30 incident:
    # Original limit short at 4547.38 entry, SL 4553.95. Price was rallying.
    # Recent swing high/low from the rally: high=4549, low=4543.
    return BreakConfirmInputs(
        direction="short",
        original_entry=4547.38,
        original_stop_loss=4553.95,
        original_take_profit=4539.50,
        atr=4.0,
        swing_high=4549.0,
        swing_low=4543.0,
        counter_trend=True,
    )


def test_converter_disabled_passes_through():
    c = BreakConfirmEntry(config=BreakConfirmEntryConfig(enabled=False))
    d = c.evaluate(_short_chase_inputs())
    assert d.converted is False
    assert d.reason == "disabled"


def test_converter_emits_stop_below_swing_low_for_chase_short():
    cfg = BreakConfirmEntryConfig(enabled=True, buffer_atr_mult=0.10, sl_buffer_atr_mult=1.0, tp_rr_target=1.2)
    c = BreakConfirmEntry(config=cfg)
    d = c.evaluate(_short_chase_inputs())
    # buffer = 0.10 * 4 = 0.4; new_entry = 4543 - 0.4 = 4542.6
    # sl_buffer = 1.0 * 4 = 4; new_sl = 4549 + 4 = 4553
    # risk = 10.4; new_tp = 4542.6 - 10.4*1.2 = 4530.12
    assert d.converted is True
    assert d.new_entry_type == "stop"
    assert d.new_entry == 4542.6
    assert d.new_stop_loss == 4553.0
    assert abs(d.new_take_profit - 4530.12) < 1e-4
    assert d.new_rr == 1.2
    assert "break_confirm_stop" in d.reason


def test_converter_emits_stop_above_swing_high_for_chase_long():
    cfg = BreakConfirmEntryConfig(enabled=True, buffer_atr_mult=0.10, sl_buffer_atr_mult=1.0, tp_rr_target=1.5)
    c = BreakConfirmEntry(config=cfg)
    inputs = BreakConfirmInputs(
        direction="long", original_entry=4546.0, original_stop_loss=4540.0,
        original_take_profit=4555.0, atr=4.0, swing_high=4549.0, swing_low=4543.0,
        counter_trend=True,
    )
    d = c.evaluate(inputs)
    # new_entry = 4549 + 0.4 = 4549.4; new_sl = 4543 - 4 = 4539
    # risk = 10.4; tp = 4549.4 + 10.4*1.5 = 4565.0
    assert d.converted is True
    assert d.new_entry == 4549.4
    assert d.new_stop_loss == 4539.0
    assert abs(d.new_take_profit - 4565.0) < 1e-4


def test_converter_skips_when_not_counter_trend():
    cfg = BreakConfirmEntryConfig(enabled=True, only_counter_trend=True)
    c = BreakConfirmEntry(config=cfg)
    inputs = BreakConfirmInputs(
        direction="short", original_entry=4547.38, original_stop_loss=4553.95,
        original_take_profit=4539.50, atr=4.0, swing_high=4549.0, swing_low=4543.0,
        counter_trend=False,
    )
    d = c.evaluate(inputs)
    assert d.converted is False
    assert d.reason == "not_counter_trend"


def test_converter_can_be_unconditional():
    cfg = BreakConfirmEntryConfig(enabled=True, only_counter_trend=False)
    c = BreakConfirmEntry(config=cfg)
    inputs = BreakConfirmInputs(
        direction="short", original_entry=4547.38, original_stop_loss=4553.95,
        original_take_profit=4539.50, atr=4.0, swing_high=4549.0, swing_low=4543.0,
        counter_trend=False,
    )
    d = c.evaluate(inputs)
    assert d.converted is True


def test_converter_rejects_invalid_atr():
    cfg = BreakConfirmEntryConfig(enabled=True)
    c = BreakConfirmEntry(config=cfg)
    inputs = BreakConfirmInputs(
        direction="short", original_entry=4547.38, original_stop_loss=4553.95,
        original_take_profit=4539.50, atr=0.0, swing_high=4549.0, swing_low=4543.0,
        counter_trend=True,
    )
    d = c.evaluate(inputs)
    assert d.converted is False
    assert d.reason == "atr_invalid"


def test_converter_rejects_invalid_swing():
    cfg = BreakConfirmEntryConfig(enabled=True)
    c = BreakConfirmEntry(config=cfg)
    inputs = BreakConfirmInputs(
        direction="short", original_entry=4547.38, original_stop_loss=4553.95,
        original_take_profit=4539.50, atr=4.0, swing_high=4540.0, swing_low=4549.0,  # inverted
        counter_trend=True,
    )
    d = c.evaluate(inputs)
    assert d.converted is False
    assert d.reason == "swing_invalid"


def test_converter_rejects_when_risk_too_large():
    # Huge swing range vs ATR → risk would exceed 3.5×ATR cap.
    cfg = BreakConfirmEntryConfig(enabled=True, max_risk_distance_atr_mult=3.5)
    c = BreakConfirmEntry(config=cfg)
    inputs = BreakConfirmInputs(
        direction="short", original_entry=4547.38, original_stop_loss=4553.95,
        original_take_profit=4539.50, atr=2.0,
        swing_high=4555.0, swing_low=4543.0,  # 12pt range, ATR=2 → risk ~14pt > 7pt cap
        counter_trend=True,
    )
    d = c.evaluate(inputs)
    assert d.converted is False
    assert "risk_too_large" in d.reason


def test_converter_rejects_when_min_rr_not_met():
    cfg = BreakConfirmEntryConfig(enabled=True, tp_rr_target=0.5, min_rr=0.8)
    c = BreakConfirmEntry(config=cfg)
    d = c.evaluate(_short_chase_inputs())
    assert d.converted is False
    assert "rr_too_low" in d.reason
