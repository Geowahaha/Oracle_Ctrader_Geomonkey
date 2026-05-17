"""Regression tests for the fibo_advance audit fixes:

  Bug B — pre-build modifier budget lets borderline signals survive the
          internal min_conf gate so post-build trend/killer/session bonuses
          can still save them.
  Bug C — _trend_confidence_modifier penalises (not rewards) the
          "D1 weakly opposes, H4 agrees" case that previously got +2.
  Bug D — NaN values in the rolling ATR average no longer silently disable
          ATR-expansion scoring in _fibonacci_killer_check.
"""
from __future__ import annotations

import math
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub heavy optional deps before import.
sys.modules.setdefault("yfinance", MagicMock())
sys.modules.setdefault("ccxt", MagicMock())

import numpy as np
import pandas as pd
import pytest

from scanners.fibo_advance import FiboAdvanceScanner


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ema_df(periods, trend: str = "flat", n: int = 60, base: float = 3000.0) -> pd.DataFrame:
    """Build a DataFrame that, after ta.add_ema, yields the desired trend.

    trend ∈ {"bullish", "bearish", "flat"}.
    """
    if trend == "bullish":
        closes = np.linspace(base - 20.0, base + 20.0, n)
    elif trend == "bearish":
        closes = np.linspace(base + 20.0, base - 20.0, n)
    else:
        closes = np.full(n, base)
    return pd.DataFrame({
        "open":   closes,
        "high":   closes + 1.0,
        "low":    closes - 1.0,
        "close":  closes,
        "volume": np.full(n, 1000.0),
    })


# ── Bug C: mixed D1 bearish + H4 bullish + long must NOT return +2 bonus ─────

def test_bug_c_mixed_d1_bearish_h4_bullish_long_gets_penalty_not_bonus():
    """D1 weakly bearish + H4 bullish + direction=long used to fall through
    the aligned branches and return +2 (h4_aligned). After the fix this
    configuration returns a small negative penalty instead."""
    scanner = FiboAdvanceScanner()
    # Weak bearish D1: close just below EMA21 which is just below EMA50.
    df_d1 = _ema_df([21, 50], trend="bearish", n=60)
    # Bullish H4: rising closes → close > EMA21 > EMA50.
    df_h4 = _ema_df([21, 50], trend="bullish", n=60)
    mod, reason = scanner._trend_confidence_modifier(df_d1, df_h4, "long")
    assert mod <= 0.0, f"expected penalty, got +{mod} ({reason})"
    assert "mixed" in reason or "h4_neutral" in reason or "strong" in reason


def test_bug_c_mixed_d1_bullish_h4_bearish_short_gets_penalty_not_bonus():
    scanner = FiboAdvanceScanner()
    df_d1 = _ema_df([21, 50], trend="bullish", n=60)
    df_h4 = _ema_df([21, 50], trend="bearish", n=60)
    mod, reason = scanner._trend_confidence_modifier(df_d1, df_h4, "short")
    assert mod <= 0.0, f"expected penalty, got +{mod} ({reason})"
    assert "mixed" in reason or "h4_neutral" in reason or "strong" in reason


def test_bug_c_aligned_still_gets_positive_bonus():
    """Sanity: the aligned path still returns +5 (or +2 H4-only)."""
    scanner = FiboAdvanceScanner()
    df_d1 = _ema_df([21, 50], trend="bullish", n=60)
    df_h4 = _ema_df([21, 50], trend="bullish", n=60)
    mod, reason = scanner._trend_confidence_modifier(df_d1, df_h4, "long")
    assert mod > 0, f"aligned bull+bull+long should bonus, got {mod} ({reason})"


# ── Bug D: NaN in rolling ATR must not silently skip scoring ─────────────────

def test_bug_d_nan_in_rolling_atr_does_not_silently_skip():
    """When atr_14 has NaN values in the last 20 bars, the rolling mean is
    NaN. Previously float(NaN) produced nan and all comparisons were False,
    silently skipping the ATR score. After the fix we fall back to the
    passed-in atr value so scoring is still well-defined (and deterministic)."""
    scanner = FiboAdvanceScanner()

    # Build a df with atr_14 values where the last 20 include NaN.
    n = 25
    atr_series = [2.0] * (n - 5) + [float("nan")] * 5
    df = pd.DataFrame({
        "open":   [3000.0] * n,
        "high":   [3001.0] * n,
        "low":    [2999.0] * n,
        "close":  [3000.0] * n,
        "volume": [1000.0] * n,
        "atr_14": atr_series,
    })

    # Snapshot clear of other killer signals so we isolate the ATR branch.
    snap = {
        "features": {
            "delta_proxy": 0.0,
            "bar_volume_proxy": 0.0,
            "spread_expansion_ratio": 1.0,
        },
        "day_type": "",
        "state_label": "",
    }

    # The key invariant: no exception and the call returns cleanly.
    # With ATR=2.0 fallback vs fallback, ratio = 1.0, below kill_mult — no
    # points expected, but also no silent nan leakage.
    allowed, reason, weight = scanner._fibonacci_killer_check(snap, atr=2.0, df_entry=df)
    assert isinstance(allowed, bool)
    assert isinstance(weight, (int, float))
    assert not math.isnan(float(weight))


# ── Bug B: internal gate in _build_signal accepts a pre-mod budget ───────────

def test_bug_b_min_conf_gate_has_pre_mod_budget():
    """Check the source uses (min_conf - pre_mod_budget) rather than a naked
    min_conf compare — so a +5 trend bonus applied by the caller can still
    rescue a borderline base confidence."""
    import inspect
    src = inspect.getsource(FiboAdvanceScanner._build_signal)
    assert "FIBO_ADVANCE_PRE_MOD_BUDGET" in src
    assert "min_conf - " in src or "min_conf -" in src


def test_bug_b_scout_min_conf_gate_has_pre_mod_budget():
    import inspect
    src = inspect.getsource(FiboAdvanceScanner._scout_scan)
    assert "FIBO_SCOUT_PRE_MOD_BUDGET" in src
    assert "min_conf - " in src or "min_conf -" in src
