"""Tests for EntryQualityRouter (KILL / MARKET / PASS routing)."""
from __future__ import annotations

from analysis.entry_quality_router import (
    ACTION_KILL,
    ACTION_MARKET,
    ACTION_PASS,
    EntryQualityInputs,
    EntryQualityRouter,
    EntryQualityRouterConfig,
)


def _high_quality_short() -> EntryQualityInputs:
    """A textbook-strong short: all evidence aligned, structure broke."""
    return EntryQualityInputs(
        direction="short",
        confidence=80.0,
        current_price=4543.5,
        original_entry=4547.0,
        original_stop_loss=4553.0,
        original_take_profit=4535.0,
        atr=4.0,
        m1_trend_aligned=True,
        m5_trend_aligned=True,
        delta_confirms=True,
        flow_confirmed=True,
        has_structure_break=True,
        counter_trend=False,
        has_anchor=True,
    )


def _mid_quality_short() -> EntryQualityInputs:
    """Borderline: confidence OK, trend partial, no break yet."""
    return EntryQualityInputs(
        direction="short",
        confidence=68.0,
        current_price=4547.5,
        original_entry=4547.0,
        original_stop_loss=4553.0,
        original_take_profit=4540.0,
        atr=4.0,
        m1_trend_aligned=True,
        m5_trend_aligned=False,
        delta_confirms=False,
        flow_confirmed=False,
        has_structure_break=False,
        counter_trend=False,
        has_anchor=True,
    )


def _chase_short_no_anchor() -> EntryQualityInputs:
    """Mirror of the -$70 loss: counter-trend short with no anchor."""
    return EntryQualityInputs(
        direction="short",
        confidence=58.0,
        current_price=4548.5,
        original_entry=4547.38,
        original_stop_loss=4553.95,
        original_take_profit=4539.5,
        atr=4.0,
        m1_trend_aligned=False,
        m5_trend_aligned=False,
        delta_confirms=False,
        flow_confirmed=False,
        has_structure_break=False,
        counter_trend=True,
        has_anchor=False,
    )


def test_router_disabled_returns_pass():
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=False))
    d = r.evaluate(_high_quality_short())
    assert d.action == ACTION_PASS
    assert d.reason == "disabled"


def test_router_kills_low_confidence():
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=True, kill_confidence_floor=60.0))
    inputs = _chase_short_no_anchor()  # conf=58.0
    d = r.evaluate(inputs)
    assert d.action == ACTION_KILL
    assert "confidence_below_floor" in d.reason


def test_router_kills_counter_trend_without_anchor():
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=True, kill_confidence_floor=50.0))
    inputs = _chase_short_no_anchor()  # counter_trend=True, has_anchor=False, conf 58 above 50 floor
    d = r.evaluate(inputs)
    assert d.action == ACTION_KILL
    assert d.reason == "counter_trend_without_anchor"


def test_router_passes_counter_trend_with_anchor():
    # Anchor present (rejection wick) — downstream BreakConfirm will handle the conversion.
    cfg = EntryQualityRouterConfig(enabled=True, kill_confidence_floor=50.0, kill_counter_trend_when_no_anchor=True)
    r = EntryQualityRouter(config=cfg)
    inputs = EntryQualityInputs(
        direction="short", confidence=65.0, current_price=4548.0,
        original_entry=4547.0, original_stop_loss=4553.0, original_take_profit=4540.0,
        atr=4.0, m1_trend_aligned=False, m5_trend_aligned=False,
        delta_confirms=False, flow_confirmed=False, has_structure_break=False,
        counter_trend=True, has_anchor=True,
    )
    d = r.evaluate(inputs)
    assert d.action == ACTION_PASS


def test_router_upgrades_high_quality_short_to_market():
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=True, market_confidence_threshold=75.0))
    d = r.evaluate(_high_quality_short())
    assert d.action == ACTION_MARKET
    assert d.new_entry_type == "market"
    # MARKET upgrade fires at the current price, not the limit price.
    assert d.new_entry == 4543.5
    assert d.market_score == 6  # all 6 conditions met


def test_router_upgrades_high_quality_long_to_market():
    inputs = EntryQualityInputs(
        direction="long", confidence=80.0, current_price=4546.5,
        original_entry=4543.0, original_stop_loss=4540.0, original_take_profit=4555.0,
        atr=4.0, m1_trend_aligned=True, m5_trend_aligned=True,
        delta_confirms=True, flow_confirmed=True, has_structure_break=True,
        counter_trend=False, has_anchor=True,
    )
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=True))
    d = r.evaluate(inputs)
    assert d.action == ACTION_MARKET
    assert d.new_entry == 4546.5


def test_router_passes_mid_quality_through_to_downstream():
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=True))
    d = r.evaluate(_mid_quality_short())
    assert d.action == ACTION_PASS
    assert "pass_through" in d.reason


def test_router_quorum_mode_allows_market_with_4_of_6():
    # require_all_market_conditions=False with quorum=4
    cfg = EntryQualityRouterConfig(
        enabled=True, require_all_market_conditions=False, market_quorum=4,
    )
    r = EntryQualityRouter(config=cfg)
    # Modify the high-quality input to drop delta_confirms + flow_confirmed → 4/6 left
    inputs = EntryQualityInputs(
        direction="short", confidence=80.0, current_price=4543.5,
        original_entry=4547.0, original_stop_loss=4553.0, original_take_profit=4535.0,
        atr=4.0, m1_trend_aligned=True, m5_trend_aligned=True,
        delta_confirms=False, flow_confirmed=False, has_structure_break=True,
        counter_trend=False, has_anchor=True,
    )
    d = r.evaluate(inputs)
    assert d.action == ACTION_MARKET
    assert d.market_score == 4


def test_router_bad_direction_passes_safely():
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=True))
    inputs = EntryQualityInputs(
        direction="unknown", confidence=80.0, current_price=4543.5,
        original_entry=4547.0, original_stop_loss=4553.0, original_take_profit=4535.0,
        atr=4.0, m1_trend_aligned=True, m5_trend_aligned=True,
        delta_confirms=True, flow_confirmed=True, has_structure_break=True,
        counter_trend=False, has_anchor=True,
    )
    d = r.evaluate(inputs)
    assert d.action == ACTION_PASS
    assert d.reason == "bad_direction"


def test_router_preserves_original_sl_tp_on_market_upgrade():
    r = EntryQualityRouter(config=EntryQualityRouterConfig(enabled=True))
    d = r.evaluate(_high_quality_short())
    assert d.action == ACTION_MARKET
    # Even though entry moves to current, SL and TP from original signal are preserved.
    assert d.new_stop_loss == 4553.0
    assert d.new_take_profit == 4535.0
