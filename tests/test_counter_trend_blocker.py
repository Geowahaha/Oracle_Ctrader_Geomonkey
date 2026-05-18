"""Tests for the CounterTrendBlocker.

Mirrors the 2026-05-18 incident: three M1 bull candles preceded a short
scalp that lost $70.23. The blocker should fire on that exact pattern.
"""
from __future__ import annotations

from analysis.counter_trend_block import (
    BlockDecision,
    CounterTrendBlocker,
    CounterTrendBlockerConfig,
    M1Candle,
)


def _bull(open_, close, high=None, low=None):
    high = high if high is not None else max(open_, close) + 0.2
    low = low if low is not None else min(open_, close) - 0.2
    return M1Candle(open=open_, close=close, high=high, low=low)


def _bear(open_, close, high=None, low=None):
    return _bull(open_, close, high, low)


def _flat(price):
    return M1Candle(open=price, close=price + 0.01, high=price + 0.1, low=price - 0.1)


def test_blocker_disabled_never_blocks():
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=False))
    d = b.evaluate(direction="short", m1_candles=[_bull(4540, 4543), _bull(4543, 4545), _bull(4545, 4548)], atr=4.0)
    assert d.blocked is False
    assert d.reason == "disabled"


def test_blocker_fires_on_short_into_3_bull_candles_matching_incident():
    # 2026-05-18 07:13-07:28 — three +3pt bull candles led to short losing $70.
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3, min_body_atr_ratio=0.30))
    candles = [_bull(4540, 4543), _bull(4543, 4546), _bull(4546, 4549)]
    d = b.evaluate(direction="short", m1_candles=candles, atr=4.0)
    assert d.blocked is True
    assert "bull_streak=3/3" in d.reason
    assert d.bull_count == 3
    assert d.bear_count == 0


def test_blocker_fires_on_long_into_3_bear_candles():
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3, min_body_atr_ratio=0.30))
    candles = [_bear(4549, 4546), _bear(4546, 4543), _bear(4543, 4540)]
    d = b.evaluate(direction="long", m1_candles=candles, atr=4.0)
    assert d.blocked is True
    assert "bear_streak=3/3" in d.reason


def test_blocker_does_not_block_with_chop():
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3))
    candles = [_bull(4540, 4543), _bear(4543, 4540), _bull(4540, 4543)]
    d = b.evaluate(direction="short", m1_candles=candles, atr=4.0)
    assert d.blocked is False


def test_blocker_does_not_block_short_when_3_bears():
    # Short with bearish candles = trend-aligned, not counter-trend.
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3))
    candles = [_bear(4549, 4546), _bear(4546, 4543), _bear(4543, 4540)]
    d = b.evaluate(direction="short", m1_candles=candles, atr=4.0)
    assert d.blocked is False


def test_blocker_does_not_fire_on_thin_bodies():
    # 3 bull candles but bodies are tiny vs ATR → not an impulse, allow.
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3, min_body_atr_ratio=0.50))
    # bodies ~0.05; atr=4.0 → ratio 0.0125 < 0.50.
    candles = [
        _bull(4540, 4540.05, high=4540.30, low=4539.90),
        _bull(4540.05, 4540.10, high=4540.40, low=4540.00),
        _bull(4540.10, 4540.15, high=4540.50, low=4540.05),
    ]
    d = b.evaluate(direction="short", m1_candles=candles, atr=4.0)
    assert d.blocked is False


def test_blocker_insufficient_candles_passes():
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3))
    d = b.evaluate(direction="short", m1_candles=[_bull(4540, 4543)], atr=4.0)
    assert d.blocked is False
    assert d.reason == "insufficient_candles"


def test_blocker_zero_atr_passes_safely():
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3))
    d = b.evaluate(direction="short", m1_candles=[_bull(4540, 4543), _bull(4543, 4546), _bull(4546, 4549)], atr=0.0)
    assert d.blocked is False
    assert d.reason == "atr_invalid"


def test_blocker_partial_streak_below_threshold_passes():
    # Default require_full_streak=True; 2 bulls out of 3 must NOT block.
    b = CounterTrendBlocker(config=CounterTrendBlockerConfig(enabled=True, lookback=3))
    candles = [_bull(4540, 4543), _bull(4543, 4546), _bear(4546, 4543)]
    d = b.evaluate(direction="short", m1_candles=candles, atr=4.0)
    assert d.blocked is False


def test_blocker_relaxed_streak_can_be_configured():
    # require_full_streak=False with min_streak_fraction=0.66 → 2/3 enough.
    cfg = CounterTrendBlockerConfig(
        enabled=True, lookback=3, require_full_streak=False, min_streak_fraction=0.66,
    )
    b = CounterTrendBlocker(config=cfg)
    candles = [_bull(4540, 4543), _bull(4543, 4546), _bear(4546, 4543)]
    d = b.evaluate(direction="short", m1_candles=candles, atr=4.0)
    assert d.blocked is True
    assert "bull_streak=2/3" in d.reason
