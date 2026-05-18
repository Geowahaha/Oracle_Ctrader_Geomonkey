"""Tests for FiboGoldenBreakEntry."""
from __future__ import annotations

from analysis.fibo_golden_break import (
    ACTION_LIVE_MARKET,
    ACTION_SKIP,
    ACTION_WAIT_BREAK_STOP,
    FiboGoldenBreakConfig,
    FiboGoldenBreakEntry,
    FiboGoldenInputs,
)


def _long_inputs(*, current_price: float, last_close: float, micro_high: float, micro_low: float) -> FiboGoldenInputs:
    return FiboGoldenInputs(
        direction="long",
        impulse_high=4560.0,
        impulse_low=4540.0,
        current_price=current_price,
        atr=2.0,
        recent_micro_high=micro_high,
        recent_micro_low=micro_low,
        last_m1_close=last_close,
        original_entry=4547.5,
        original_stop_loss=4540.0,
        original_take_profit=4560.0,
    )


def _short_inputs(*, current_price: float, last_close: float, micro_high: float, micro_low: float) -> FiboGoldenInputs:
    return FiboGoldenInputs(
        direction="short",
        impulse_high=4560.0,
        impulse_low=4540.0,
        current_price=current_price,
        atr=2.0,
        recent_micro_high=micro_high,
        recent_micro_low=micro_low,
        last_m1_close=last_close,
        original_entry=4552.5,
        original_stop_loss=4560.0,
        original_take_profit=4540.0,
    )


def test_disabled_skip_with_reason():
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=False))
    d = g.evaluate(_long_inputs(current_price=4548, last_close=4548, micro_high=4549, micro_low=4546))
    assert d.action == ACTION_SKIP
    assert d.reason == "disabled"


def test_long_break_confirmed_upgrades_to_live_market():
    # ratio = (4560 - 4547) / 20 = 0.65 → inside 0.50-0.88 zone for LONG
    # last_close=4549 > micro_high=4548 → break confirmed
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=True))
    d = g.evaluate(_long_inputs(current_price=4547, last_close=4549, micro_high=4548, micro_low=4545))
    assert d.action == ACTION_LIVE_MARKET
    assert d.entry_type == "market"
    assert d.new_entry == 4547.0
    assert d.new_stop_loss == 4540.0  # original SL preserved
    assert d.new_take_profit == 4560.0
    assert d.break_confirmed is True
    assert d.inside_golden_zone is True
    assert 0.5 <= d.ratio_at_price <= 0.88


def test_long_inside_zone_no_break_converts_to_wait_break_stop():
    # ratio inside zone, but last_close did not break above micro_high.
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(
        enabled=True, stop_trigger_buffer_atr=0.10, sl_buffer_atr=1.0, tp_rr_target=1.5,
    ))
    d = g.evaluate(_long_inputs(current_price=4547, last_close=4546.5, micro_high=4548, micro_low=4544))
    assert d.action == ACTION_WAIT_BREAK_STOP
    assert d.entry_type == "stop"
    # buy_stop entry = micro_high + 0.10*ATR = 4548 + 0.2 = 4548.2
    assert d.new_entry == 4548.2
    # SL = micro_low - 1.0*ATR = 4544 - 2 = 4542; risk = 6.2
    assert d.new_stop_loss == 4542.0
    # TP = entry + risk*1.5 = 4548.2 + 9.3 = 4557.5
    assert d.new_take_profit == 4557.5
    assert d.break_confirmed is False
    assert d.inside_golden_zone is True


def test_outside_golden_zone_skips():
    # current_price=4558 → ratio = (4560-4558)/20 = 0.10 → outside 0.50-0.88 zone
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=True))
    d = g.evaluate(_long_inputs(current_price=4558, last_close=4558, micro_high=4559, micro_low=4557))
    assert d.action == ACTION_SKIP
    assert "outside_golden_zone" in d.reason
    assert d.inside_golden_zone is False


def test_too_deep_retracement_skips():
    # current_price=4541 → ratio = (4560-4541)/20 = 0.95 → past 0.88 → skip
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=True, zone_high=0.88))
    d = g.evaluate(_long_inputs(current_price=4541, last_close=4541, micro_high=4542, micro_low=4540))
    assert d.action == ACTION_SKIP
    assert "outside_golden_zone" in d.reason


def test_short_break_confirmed_live_market():
    # SHORT after bearish impulse: ratio = (price - low) / range
    # ratio = (4553 - 4540) / 20 = 0.65 → inside
    # last_close=4551 < micro_low=4552 → break confirmed for SHORT
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=True))
    d = g.evaluate(_short_inputs(current_price=4553, last_close=4551, micro_high=4555, micro_low=4552))
    assert d.action == ACTION_LIVE_MARKET
    assert d.entry_type == "market"
    assert d.new_entry == 4553.0


def test_short_inside_zone_no_break_converts_to_sell_stop():
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(
        enabled=True, stop_trigger_buffer_atr=0.10, sl_buffer_atr=1.0, tp_rr_target=1.5,
    ))
    d = g.evaluate(_short_inputs(current_price=4553, last_close=4553.5, micro_high=4555, micro_low=4552))
    assert d.action == ACTION_WAIT_BREAK_STOP
    assert d.entry_type == "stop"
    # sell_stop = micro_low - 0.10*ATR = 4552 - 0.2 = 4551.8
    assert d.new_entry == 4551.8
    # SL = micro_high + 1.0*ATR = 4555 + 2 = 4557
    assert d.new_stop_loss == 4557.0


def test_invalid_atr_passes_safely():
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=True))
    inputs = FiboGoldenInputs(
        direction="long", impulse_high=4560.0, impulse_low=4540.0,
        current_price=4548.0, atr=0.0,
        recent_micro_high=4548.0, recent_micro_low=4545.0, last_m1_close=4548.0,
        original_entry=4548.0, original_stop_loss=4545.0, original_take_profit=4560.0,
    )
    d = g.evaluate(inputs)
    assert d.action == ACTION_SKIP
    assert d.reason == "atr_invalid"


def test_invalid_impulse_zone_passes_safely():
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=True))
    inputs = FiboGoldenInputs(
        direction="long", impulse_high=4540.0, impulse_low=4560.0,  # inverted
        current_price=4548.0, atr=2.0,
        recent_micro_high=4548.0, recent_micro_low=4545.0, last_m1_close=4548.0,
        original_entry=4548.0, original_stop_loss=4545.0, original_take_profit=4560.0,
    )
    d = g.evaluate(inputs)
    assert d.action == ACTION_SKIP
    assert d.reason == "zone_invalid_pass"


def test_risk_too_large_skips():
    # Tiny ATR=0.5 but micro range 4540..4548 = 8pt → risk would exceed 4.5×ATR cap.
    g = FiboGoldenBreakEntry(config=FiboGoldenBreakConfig(enabled=True, max_risk_atr_mult=4.5))
    inputs = FiboGoldenInputs(
        direction="long", impulse_high=4560.0, impulse_low=4540.0,
        current_price=4547.0, atr=0.5,
        recent_micro_high=4548.0, recent_micro_low=4540.0,
        last_m1_close=4546.5,
        original_entry=4547.0, original_stop_loss=4540.0, original_take_profit=4560.0,
    )
    d = g.evaluate(inputs)
    assert d.action == ACTION_SKIP
    assert "risk_too_large" in d.reason
