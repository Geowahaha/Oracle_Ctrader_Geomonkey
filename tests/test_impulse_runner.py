"""Tests for the Impulse Runner engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from learning.impulse_runner import (
    ImpulseRunner,
    ImpulseRunnerConfig,
    PositionSnapshot,
    StructureBreakSignal,
)


_T0 = datetime(2026, 5, 18, 6, 0, 0, tzinfo=timezone.utc)


def _clock_factory(start: datetime):
    state = {"now": start}

    def now() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


def _short_winning() -> PositionSnapshot:
    # Entry 4550, SL 4555 (5pt risk), price moved to 4540 → MFE 10/5 = 2R
    return PositionSnapshot(
        position_id=42,
        symbol="XAUUSD",
        direction="short",
        entry_price=4550.0,
        current_price=4540.0,
        stop_loss=4555.0,
        take_profit=4545.0,
        atr_5m=4.0,
        volume=0.1,
        source="scalp_xauusd:winner",
    )


def _strong_break_aligned() -> StructureBreakSignal:
    return StructureBreakSignal(
        aligned_with_position=True,
        strength=0.80,
        last_5m_close_break=True,
        delta_confirms=True,
    )


def test_runner_disabled_returns_none():
    r = ImpulseRunner(config=ImpulseRunnerConfig(enabled=False))
    d = r.evaluate(position=_short_winning(), structure=_strong_break_aligned())
    assert d is None


def test_runner_fires_when_mfe_and_structure_pass():
    clock, _ = _clock_factory(_T0)
    r = ImpulseRunner(config=ImpulseRunnerConfig(enabled=True, min_mfe_r=1.0), clock=clock)
    d = r.evaluate(position=_short_winning(), structure=_strong_break_aligned())
    assert d is not None
    # For short: new SL above current price, new TP below current price.
    assert d.new_stop_loss > 4540.0
    assert d.new_take_profit < 4540.0
    # SL never moves backward.
    assert d.new_stop_loss <= 4555.0
    # TP never shrinks (only extends).
    assert d.new_take_profit <= 4545.0
    assert "impulse_runner" in d.reason


def test_runner_does_not_fire_when_mfe_below_threshold():
    clock, _ = _clock_factory(_T0)
    pos = _short_winning()
    # Move price back so MFE is only 0.5R.
    pos = PositionSnapshot(
        position_id=pos.position_id, symbol=pos.symbol, direction=pos.direction,
        entry_price=4550.0, current_price=4547.5, stop_loss=4555.0, take_profit=4545.0,
        atr_5m=4.0, volume=0.1, source=pos.source,
    )
    r = ImpulseRunner(config=ImpulseRunnerConfig(enabled=True, min_mfe_r=1.0), clock=clock)
    assert r.evaluate(position=pos, structure=_strong_break_aligned()) is None


def test_runner_does_not_fire_when_structure_misaligned():
    r = ImpulseRunner(config=ImpulseRunnerConfig(enabled=True))
    structure = StructureBreakSignal(
        aligned_with_position=False, strength=0.90,
        last_5m_close_break=True, delta_confirms=True,
    )
    assert r.evaluate(position=_short_winning(), structure=structure) is None


def test_runner_does_not_fire_when_break_strength_too_low():
    r = ImpulseRunner(config=ImpulseRunnerConfig(enabled=True, min_break_strength=0.5))
    structure = StructureBreakSignal(
        aligned_with_position=True, strength=0.30,
        last_5m_close_break=True, delta_confirms=True,
    )
    assert r.evaluate(position=_short_winning(), structure=structure) is None


def test_runner_respects_delta_and_5m_break_requirements():
    cfg = ImpulseRunnerConfig(enabled=True, require_delta_confirms=True, require_5m_break_close=True)
    r = ImpulseRunner(config=cfg)
    s_no_delta = StructureBreakSignal(True, 0.80, True, False)
    s_no_close = StructureBreakSignal(True, 0.80, False, True)
    assert r.evaluate(position=_short_winning(), structure=s_no_delta) is None
    assert r.evaluate(position=_short_winning(), structure=s_no_close) is None


def test_runner_cooldown_blocks_back_to_back():
    clock, advance = _clock_factory(_T0)
    cfg = ImpulseRunnerConfig(enabled=True, cooldown_seconds=60.0)
    r = ImpulseRunner(config=cfg, clock=clock)
    d1 = r.evaluate(position=_short_winning(), structure=_strong_break_aligned())
    assert d1 is not None
    d2 = r.evaluate(position=_short_winning(), structure=_strong_break_aligned())
    assert d2 is None
    advance(120.0)
    d3 = r.evaluate(position=_short_winning(), structure=_strong_break_aligned())
    assert d3 is not None


def test_runner_long_direction_emits_correct_trail_and_extend():
    clock, _ = _clock_factory(_T0)
    pos = PositionSnapshot(
        position_id=99, symbol="XAUUSD", direction="long",
        entry_price=4500.0, current_price=4510.0, stop_loss=4495.0, take_profit=4505.0,
        atr_5m=4.0, volume=0.1, source="scalp_xauusd:winner",
    )
    r = ImpulseRunner(config=ImpulseRunnerConfig(enabled=True, min_mfe_r=1.0), clock=clock)
    d = r.evaluate(position=pos, structure=_strong_break_aligned())
    assert d is not None
    # For long: new SL below current price, new TP above.
    assert d.new_stop_loss < 4510.0
    assert d.new_take_profit > 4510.0
    # SL never moves down past original.
    assert d.new_stop_loss >= 4495.0
    # TP never shrinks below original.
    assert d.new_take_profit >= 4505.0


def test_runner_allowed_sources_filter():
    cfg = ImpulseRunnerConfig(enabled=True, allowed_sources_csv="scalp_xauusd:winner")
    r = ImpulseRunner(config=cfg)
    pos = _short_winning()
    # Source not in whitelist.
    pos_other = PositionSnapshot(
        position_id=pos.position_id, symbol=pos.symbol, direction=pos.direction,
        entry_price=pos.entry_price, current_price=pos.current_price,
        stop_loss=pos.stop_loss, take_profit=pos.take_profit,
        atr_5m=pos.atr_5m, volume=pos.volume, source="fibo_xauusd",
    )
    assert r.evaluate(position=pos_other, structure=_strong_break_aligned()) is None
    assert r.evaluate(position=pos, structure=_strong_break_aligned()) is not None
