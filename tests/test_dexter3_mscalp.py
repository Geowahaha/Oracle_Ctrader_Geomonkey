"""Pins dexter3/mscalp.py — the 5-minute impulse-continuation scalper
(owner order 2026-08-05). Entry triggers, drift alignment + regime gate,
geometry, off-by-default flag, runner isolation."""
from __future__ import annotations

import pytest

from dexter3 import mscalp as ms


def _bar(ts, o, h, l, c, v=30.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _ts(i):
    return f"2026-08-05T{i // 12:02d}:{(i % 12) * 5:02d}:00Z"


def _drifting(n, start_px=28000.0, step=1.2):
    """Gentle uptrend: small bodies (never impulse), steady +step/bar drift."""
    bars = []
    px = start_px
    for i in range(n):
        bars.append(_bar(_ts(i), px, px + 2.5, px - 1.0, px + step))
        px += step
    return bars


def _impulse_up(i, px, atr_guess=3.5):
    rng = atr_guess * 1.6
    return _bar(_ts(i), px, px + rng, px - rng * 0.1, px + rng * 0.8)


def test_enters_on_impulse_with_drift():
    bars = _drifting(80)
    px = float(bars[-1]["close"])
    bars.append(_impulse_up(80, px))
    d = ms.decide_mscalp("USTEC", bars, spread_abs=1.5)
    assert d.action == "enter"
    assert d.side == "buy"
    assert d.setup == "mscalp_impulse"
    assert d.entry_type == "market"
    risk = d.entry - d.sl
    assert risk > 0
    assert d.tp == pytest.approx(d.entry + 3.0 * risk, abs=1e-3)


def test_skips_without_impulse():
    bars = _drifting(81)
    d = ms.decide_mscalp("USTEC", bars, spread_abs=1.5)
    assert d.action == "skip"
    assert "no_impulse" in d.reasons[0]


def test_skips_impulse_against_drift():
    bars = _drifting(80)  # up-drift
    px = float(bars[-1]["close"])
    rng = 6.0
    bars.append(_bar(_ts(80), px, px + rng * 0.1, px - rng, px - rng * 0.8))  # red impulse
    d = ms.decide_mscalp("USTEC", bars, spread_abs=1.5)
    assert d.action == "skip"
    assert "against_drift" in d.reasons[0]


def test_regime_gate_blocks_flat_market(monkeypatch):
    monkeypatch.setenv(ms.ENV_DRIFT_MIN_ATR, "50")   # absurd gate
    bars = _drifting(80)
    px = float(bars[-1]["close"])
    bars.append(_impulse_up(80, px))
    d = ms.decide_mscalp("USTEC", bars, spread_abs=1.5)
    assert d.action == "skip"
    assert "regime_flat" in d.reasons[0]


def test_mode_off_by_default(monkeypatch):
    monkeypatch.delenv("DEXTER3_MODE", raising=False)
    monkeypatch.delenv("DEXTER3_PRODUCER", raising=False)
    assert ms.mscalp_mode_enabled() is False
    monkeypatch.setenv("DEXTER3_MODE", "mscalp")
    assert ms.mscalp_mode_enabled() is True


def test_label_isolation_and_runner_resolvers(monkeypatch):
    from dexter3.executor import label_matches_family

    assert ms.MSCALP_LABEL == "dexter3:mscalp:canary"
    assert label_matches_family(ms.MSCALP_LABEL, "dexter3:mscalp") is True
    assert label_matches_family(ms.MSCALP_LABEL, "dexter3:scalp") is False
    assert label_matches_family("dexter3:scalp:canary", "dexter3:mscalp") is False

    import dexter3.shadow_runner as sr
    monkeypatch.setenv("DEXTER3_MODE", "mscalp")
    assert sr._active_order_label() == "dexter3:mscalp:canary"
    assert sr._active_label_family() == "dexter3:mscalp"
    assert sr._active_state_file().name == "dexter3_mscalp_shadow_state.json"
    assert sr._active_log_file().name == "dexter3_mscalp_shadow.log"
    assert sr._mscalp_producer_enabled() is True
    assert sr._alt_producer_enabled() is True
