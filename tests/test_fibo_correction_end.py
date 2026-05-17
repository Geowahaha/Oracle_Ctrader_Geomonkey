from __future__ import annotations

import pandas as pd

from analysis.fibonacci import FibonacciAnalyzer


def _bar(open_: float, high: float, low: float, close: float, volume: float = 100.0) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def _make_df(bars: list[dict]) -> pd.DataFrame:
    idx = pd.date_range("2026-02-01", periods=len(bars), freq="H")
    return pd.DataFrame(bars, index=idx)


def test_bullish_correction_end_boosts_confluence_and_marks_impulse_restart():
    analyzer = FibonacciAnalyzer(swing_lookback=3, min_impulse_atr_mult=0.8)
    bars: list[dict] = []
    price = 100.0
    for _ in range(20):
        bars.append(_bar(price, price + 1.2, price - 0.2, price + 1.0, 140.0))
        price += 1.0
    bars.extend(
        [
            _bar(119.5, 119.7, 117.8, 118.4, 95.0),
            _bar(118.4, 118.6, 116.9, 117.3, 88.0),
            _bar(117.3, 117.5, 115.9, 116.4, 82.0),
            _bar(116.4, 116.8, 115.4, 116.2, 78.0),
            _bar(116.2, 117.4, 115.8, 117.2, 76.0),
            _bar(117.2, 118.8, 117.0, 118.6, 74.0),
        ]
    )
    df = _make_df(bars)

    ctx = analyzer.analyze(df, df, current_price=118.6, atr=1.2)

    assert ctx.correction_end_confirmed is True
    assert ctx.correction_end_score >= 4.0
    assert ctx.wave_phase in {"correction_end", "impulse_restart"}
    assert any("correction_end_confirmed" in reason for reason in ctx.reasons)
    assert ctx.fibo_confluence_score >= 45.0


def test_bearish_correction_end_detected_on_reversal_from_retracement():
    analyzer = FibonacciAnalyzer(swing_lookback=3, min_impulse_atr_mult=0.8)
    bars: list[dict] = []
    price = 200.0
    for _ in range(20):
        bars.append(_bar(price, price + 0.2, price - 1.2, price - 1.0, 145.0))
        price -= 1.0
    bars.extend(
        [
            _bar(180.5, 182.2, 180.2, 181.8, 92.0),
            _bar(181.8, 183.4, 181.5, 183.0, 86.0),
            _bar(183.0, 184.1, 182.8, 183.8, 79.0),
            _bar(183.8, 184.6, 183.4, 183.7, 74.0),
            _bar(183.7, 184.0, 182.1, 182.5, 71.0),
            _bar(182.5, 182.8, 180.9, 181.1, 68.0),
        ]
    )
    df = _make_df(bars)

    ctx = analyzer.analyze(df, df, current_price=181.1, atr=1.2)

    assert ctx.correction_end_confirmed is True
    assert ctx.correction_end_score >= 4.0
    assert ctx.wave_phase in {"correction_end", "impulse_restart"}
    assert any("correction_reversal_bar" in reason for reason in ctx.reasons)


def test_no_correction_end_when_retracement_is_still_bleeding_without_reclaim():
    analyzer = FibonacciAnalyzer(swing_lookback=3, min_impulse_atr_mult=0.8)
    bars: list[dict] = []
    price = 100.0
    for _ in range(18):
        bars.append(_bar(price, price + 1.1, price - 0.2, price + 0.9, 130.0))
        price += 0.9
    bars.extend(
        [
            _bar(116.2, 116.4, 114.9, 115.1, 110.0),
            _bar(115.1, 115.2, 113.8, 114.0, 108.0),
            _bar(114.0, 114.2, 112.7, 113.0, 105.0),
            _bar(113.0, 113.1, 111.6, 111.9, 103.0),
            _bar(111.9, 112.0, 110.5, 110.9, 101.0),
        ]
    )
    df = _make_df(bars)

    ctx = analyzer.analyze(df, df, current_price=110.9, atr=1.2)

    assert ctx.correction_end_confirmed is False
    assert ctx.correction_end_score < 4.0
    assert ctx.wave_phase != "impulse_restart"
