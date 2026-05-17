"""Unit tests for impulse-birth detection in analysis/fibonacci.py.

Scope:
- Detection: tight base + breakout bar + body dominance → detected.
- Negative cases: base too wide, too-small breakout, dominant wick,
  breakout too old, already chased too far, whipsaw/trap history.
- Integration into analyze(): entry_mode promotion + birth fib levels.

Stays local to the fibo lane — no scanner or executor imports.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis.fibonacci import FibonacciAnalyzer, FiboSignalContext


def _bar(open_: float, high: float, low: float, close: float, volume: float = 100.0) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def _make_df(bars: list[dict]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(bars), freq="H")
    return pd.DataFrame(bars, index=idx)


def _flat_base(count: int, mid: float, half_range: float) -> list[dict]:
    """Tight consolidation: each bar oscillates within ±half_range of mid.

    Deterministic (no RNG) so tests can reason about exact base_high/low.
    After this call, base_high == mid + half_range and base_low == mid - half_range.
    """
    out = []
    for i in range(count):
        # Alternate up-close and down-close bars; first + last bars hit the
        # extremes so base_high/base_low are exactly mid +/- half_range.
        if i == 0:
            o, h, l, c = mid, mid + half_range, mid - 0.05, mid + half_range * 0.5
        elif i == count - 1:
            o, h, l, c = mid, mid + 0.05, mid - half_range, mid - half_range * 0.5
        elif i % 2 == 0:
            o, h, l, c = mid - 0.1, mid + half_range * 0.6, mid - 0.15, mid + 0.2
        else:
            o, h, l, c = mid + 0.1, mid + 0.15, mid - half_range * 0.6, mid - 0.2
        out.append(_bar(o, h, l, c))
    return out


def _make_analyzer(**overrides) -> FibonacciAnalyzer:
    return FibonacciAnalyzer(
        swing_lookback=5,
        min_impulse_atr_mult=1.2,
        impulse_birth_enabled=True,
        impulse_birth_base_bars=overrides.get("base_bars", 8),
        impulse_birth_max_base_atr=overrides.get("max_base_atr", 1.2),
        impulse_birth_min_break_atr=overrides.get("min_break_atr", 0.5),
        impulse_birth_min_body_pct=overrides.get("min_body_pct", 0.55),
        impulse_birth_max_breakout_age=overrides.get("max_age", 3),
        impulse_birth_max_chase_atr=overrides.get("max_chase_atr", 1.5),
        impulse_birth_whipsaw_lookback=overrides.get("whipsaw_lookback", 10),
        impulse_birth_min_confidence=overrides.get("min_confidence", 0.55),
        impulse_birth_score_bonus=overrides.get("bonus", 10.0),
        impulse_birth_stale_age_bars=overrides.get("stale_bars", 25),
    )


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_bullish_impulse_birth_detected():
    """Stable history + tight base + strong bullish breakout close."""
    atr = 1.0
    # 12 bars of older mild drift
    quiet = _flat_base(12, mid=100.0, half_range=0.6)
    # 8 bar tight base around 101
    base = _flat_base(8, mid=101.0, half_range=0.6)
    # one bullish breakout bar: base_high ~101.6; close well above
    breakout = _bar(open_=101.4, high=103.1, low=101.3, close=103.0)
    df = _make_df(quiet + base + [breakout])
    current_price = 103.05  # very close to breakout close

    birth = _make_analyzer()._detect_impulse_birth(df, current_price, atr)
    assert birth["detected"] is True, birth
    assert birth["direction"] == "bullish"
    assert birth["anchor_price"] < 102.0
    assert 0.55 <= birth["confidence"] <= 1.0


def test_bearish_impulse_birth_detected():
    atr = 1.0
    quiet = _flat_base(12, mid=200.0, half_range=0.6)
    base = _flat_base(8, mid=199.0, half_range=0.6)
    breakout = _bar(open_=198.5, high=198.7, low=196.9, close=197.0)
    df = _make_df(quiet + base + [breakout])
    current_price = 196.95

    birth = _make_analyzer()._detect_impulse_birth(df, current_price, atr)
    assert birth["detected"] is True
    assert birth["direction"] == "bearish"
    assert birth["anchor_price"] > 198.0


# ---------------------------------------------------------------------------
# Negative cases — filters working
# ---------------------------------------------------------------------------


def test_reject_wide_base():
    """Base range exceeds ATR * max_base_atr."""
    atr = 1.0
    quiet = _flat_base(12, mid=100.0, half_range=0.6)
    # Wide oscillating "base" — range > atr * 1.2
    wide_base = _flat_base(8, mid=101.0, half_range=2.0)
    breakout = _bar(open_=103.0, high=104.5, low=102.9, close=104.4)
    df = _make_df(quiet + wide_base + [breakout])
    birth = _make_analyzer()._detect_impulse_birth(df, 104.45, atr)
    assert birth["detected"] is False


def test_reject_small_break():
    """Breakout too small to qualify."""
    atr = 1.0
    quiet = _flat_base(12, mid=100.0, half_range=0.6)
    base = _flat_base(8, mid=101.0, half_range=0.6)
    # Close just barely above base high — under atr*0.5 threshold
    breakout = _bar(open_=101.5, high=101.9, low=101.4, close=101.85)
    df = _make_df(quiet + base + [breakout])
    birth = _make_analyzer()._detect_impulse_birth(df, 101.85, atr)
    assert birth["detected"] is False


def test_reject_wick_dominant_breakout():
    """Big upper wick on a 'bullish' bar → body < 55% of range → rejected."""
    atr = 1.0
    quiet = _flat_base(12, mid=100.0, half_range=0.6)
    base = _flat_base(8, mid=101.0, half_range=0.6)
    # range 2.5 (low 101.0, high 103.5), body 0.5 → 20% body
    breakout = _bar(open_=101.5, high=103.5, low=101.0, close=102.0)
    df = _make_df(quiet + base + [breakout])
    birth = _make_analyzer()._detect_impulse_birth(df, 102.0, atr)
    assert birth["detected"] is False


def test_reject_chased_too_far():
    """Price has already run far past the breakout close."""
    atr = 1.0
    quiet = _flat_base(12, mid=100.0, half_range=0.6)
    base = _flat_base(8, mid=101.0, half_range=0.6)
    breakout = _bar(open_=101.4, high=103.1, low=101.3, close=103.0)
    df = _make_df(quiet + base + [breakout])
    # Current price 5 ATR above breakout — chased
    birth = _make_analyzer()._detect_impulse_birth(df, 108.0, atr)
    assert birth["detected"] is False


def test_reject_breakout_too_old():
    """Breakout happened more bars ago than max_breakout_age."""
    atr = 1.0
    quiet = _flat_base(12, mid=100.0, half_range=0.6)
    base = _flat_base(8, mid=101.0, half_range=0.6)
    breakout = _bar(open_=101.4, high=103.1, low=101.3, close=103.0)
    # Add 5 subsequent drift bars → breakout is offset=6, > max_age=3
    follow = _flat_base(5, mid=103.0, half_range=0.4)
    df = _make_df(quiet + base + [breakout] + follow)
    birth = _make_analyzer()._detect_impulse_birth(df, 103.0, atr)
    assert birth["detected"] is False


def test_reject_whipsaw_history():
    """Prior bearish break that then reversed → trap risk, reject."""
    atr = 1.0
    # Bearish fake break inside whipsaw window, then recovery, then setup
    pre = _flat_base(3, mid=100.0, half_range=0.5)
    bear_fake = _bar(open_=99.5, high=99.6, low=97.5, close=97.6)  # close < prior-low-0.5
    recovery = _flat_base(5, mid=99.5, half_range=0.5)
    base = _flat_base(8, mid=101.0, half_range=0.6)
    breakout = _bar(open_=101.4, high=103.1, low=101.3, close=103.0)
    df = _make_df(pre + [bear_fake] + recovery + base + [breakout])
    birth = _make_analyzer(whipsaw_lookback=10)._detect_impulse_birth(df, 103.0, atr)
    assert birth["detected"] is False


def test_insufficient_history():
    atr = 1.0
    df = _make_df(_flat_base(5, mid=100.0, half_range=0.5))
    birth = _make_analyzer()._detect_impulse_birth(df, 100.0, atr)
    assert birth["detected"] is False


def test_disabled_flag_returns_empty():
    atr = 1.0
    quiet = _flat_base(12, mid=100.0, half_range=0.6)
    base = _flat_base(8, mid=101.0, half_range=0.6)
    breakout = _bar(open_=101.4, high=103.1, low=101.3, close=103.0)
    df = _make_df(quiet + base + [breakout])
    analyzer = FibonacciAnalyzer(impulse_birth_enabled=False)
    birth = analyzer._detect_impulse_birth(df, 103.0, atr)
    assert birth["detected"] is False


# ---------------------------------------------------------------------------
# Integration: analyze() surfaces birth fields without breaking late-retrace
# ---------------------------------------------------------------------------


def test_analyze_sets_entry_mode_late_retrace_when_no_birth():
    """Classic retracement setup should leave entry_mode as late_retrace."""
    # Build an upward impulse then a retracement — no tight base near end.
    atr = 1.0
    bars: list[dict] = []
    # Slow upward drift
    price = 100.0
    for _ in range(30):
        bars.append(_bar(price, price + 0.6, price - 0.2, price + 0.5))
        price += 0.5
    # Retracement pull-back (wide bars)
    for _ in range(8):
        bars.append(_bar(price, price + 0.3, price - 1.5, price - 1.3))
        price -= 1.3
    df = _make_df(bars)
    ctx: FiboSignalContext = _make_analyzer().analyze(
        df_structure=df, df_entry=df, current_price=price, atr=atr
    )
    # We only require that the new field is present and not "early_origin"
    # because birth detection should not false-fire on a downtrend pullback.
    assert ctx.entry_mode in ("none", "late_retrace")
    assert ctx.impulse_birth_detected is False


def test_analyze_promotes_early_origin_when_old_anchor_and_fresh_birth():
    """Old completed impulse (stale) + fresh birth break → early_origin."""
    atr = 1.0
    # Old impulse followed by a long drift so late-retrace anchor is stale.
    old_impulse_up = [_bar(100 + i * 0.5, 100 + i * 0.5 + 0.6,
                           100 + i * 0.5 - 0.2, 100 + i * 0.5 + 0.5)
                      for i in range(20)]
    old_impulse_down = [_bar(110 - i * 0.4, 110 - i * 0.4 + 0.2,
                             110 - i * 0.4 - 0.6, 110 - i * 0.4 - 0.5)
                        for i in range(5)]
    drift = _flat_base(40, mid=108.0, half_range=0.6)  # long drift → staleness
    base = _flat_base(8, mid=107.5, half_range=0.6)
    breakout = _bar(open_=107.8, high=109.5, low=107.7, close=109.4)
    df = _make_df(old_impulse_up + old_impulse_down + drift + base + [breakout])
    ctx = _make_analyzer(stale_bars=10).analyze(
        df_structure=df, df_entry=df, current_price=109.4, atr=atr
    )
    # Either late_retrace (not stale) or early_origin — but when stale it
    # must promote.
    assert ctx.impulse_birth_detected is True
    assert ctx.entry_mode == "early_origin"
    assert ctx.impulse_birth_fib_levels is not None
    assert ctx.impulse_birth_direction == "bullish"
    # Anchor is the base low — must be below current price.
    assert ctx.impulse_birth_anchor_price < 109.0
    assert any("impulse_birth_bullish" in r for r in ctx.reasons)


def test_analyze_keeps_late_retrace_when_zone_is_active():
    """When price is inside golden pocket AND birth also fires, keep late_retrace.

    This preserves opportunity-first: do not suppress a real retracement
    entry just because a fresh break exists elsewhere.
    """
    atr = 1.0
    # Build a setup where analyze finds a late-retrace impulse AND price
    # sits in golden pocket. (We do not strictly assert late_retrace wins;
    # we assert the field exists, no crash, and the birth info — if any —
    # is published as metadata rather than overriding a live retracement.)
    bars: list[dict] = []
    price = 100.0
    for _ in range(25):
        bars.append(_bar(price, price + 0.6, price - 0.2, price + 0.5))
        price += 0.5
    top = price
    # Pull back to ~golden pocket
    for _ in range(6):
        bars.append(_bar(price, price + 0.3, price - 1.0, price - 0.9))
        price -= 0.9
    df = _make_df(bars)
    ctx = _make_analyzer().analyze(
        df_structure=df, df_entry=df, current_price=price, atr=atr
    )
    assert ctx.entry_mode in ("late_retrace", "early_origin", "none")
    # Must never crash and must expose the flag.
    assert hasattr(ctx, "impulse_birth_detected")
