"""Tests for dexter3/vp_regime.py — walk-forward regime-gate building blocks."""
from __future__ import annotations

import pytest

from dexter3 import vp_regime as vr


def _bar(o, h, l, c, v=10.0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "ts": "t"}


def test_atr_series_positive_and_aligned():
    bars = [_bar(100, 101, 99, 100.5)] * 20
    atr = vr.atr_series(bars)
    assert len(atr) == 20
    assert all(a > 0 for a in atr)


def test_efficiency_ratio_trend_vs_chop():
    trend = [100 + i * 0.5 for i in range(20)]
    chop = [100 + (0.5 if i % 2 else -0.5) for i in range(20)]
    assert vr.efficiency_ratio(trend) > 0.9
    assert vr.efficiency_ratio(chop) < 0.2


def test_regime_features_none_without_history():
    bars = [_bar(100, 101, 99, 100)] * 50
    assert vr.regime_features(bars, vr.atr_series(bars), 49) is None


def test_regime_features_complete_keys():
    bars = [_bar(100 + i * 0.01, 100.6 + i * 0.01, 99.4 + i * 0.01, 100.2 + i * 0.01) for i in range(1500)]
    feats = vr.regime_features(bars, vr.atr_series(bars), len(bars) - 1)
    assert feats is not None
    assert set(feats.keys()) == set(vr.FEATURES)


def test_derive_stump_finds_separating_feature():
    # winners have eff_h1 high, losers low -> stump must pick eff_h1 >=
    trades = []
    for k in range(30):
        trades.append({"features": {f: 0.5 for f in vr.FEATURES} | {"eff_h1": 0.8}, "pnl": +1.0})
        trades.append({"features": {f: 0.5 for f in vr.FEATURES} | {"eff_h1": 0.1}, "pnl": -1.0})
    rule = vr.derive_stump(trades)
    assert rule is not None
    assert rule["feature"] == "eff_h1"
    assert rule["op"] == ">="
    assert vr.apply_stump(rule, {f: 0.5 for f in vr.FEATURES} | {"eff_h1": 0.9}) is True
    assert vr.apply_stump(rule, {f: 0.5 for f in vr.FEATURES} | {"eff_h1": 0.05}) is False


def test_derive_stump_none_when_no_separation():
    trades = [{"features": {f: 0.5 for f in vr.FEATURES}, "pnl": (1.0 if k % 2 else -1.0)} for k in range(40)]
    assert vr.derive_stump(trades) is None


def test_apply_stump_open_without_rule_closed_without_features():
    assert vr.apply_stump(None, {"eff_h1": 0.1}) is True
    assert vr.apply_stump({"feature": "eff_h1", "op": ">=", "threshold": 0.5}, None) is False


def test_codex_profile_regime_coexists():
    bars = [_bar(100 + i * 0.1, 100.7 + i * 0.1, 99.5 + i * 0.1, 100.4 + i * 0.1) for i in range(60)]
    out = vr.profile_regime(bars)
    assert out["state"] in ("directional", "rotational", "unknown")
