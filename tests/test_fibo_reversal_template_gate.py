import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

sys.modules["yfinance"] = MagicMock()
sys.modules["ccxt"] = MagicMock()

import scanners.fibo_advance as fibo_module
from scanners.fibo_advance import FiboAdvanceScanner


def _make_fibo_ctx(*, ratio=0.618, depth=0.635, in_gp=True, impulse_strength=0.74,
                   correction_end_confirmed=False, correction_end_score=0.0,
                   wave_phase="unknown", wave_confidence=0.0):
    return SimpleNamespace(
        nearest_level_ratio=ratio,
        retracement_depth=depth,
        in_golden_pocket=in_gp,
        retracement_healthy=True,
        volume_diminishing=True,
        impulse_confirmed=True,
        fib_levels=SimpleNamespace(impulse_strength=impulse_strength),
        correction_end_confirmed=correction_end_confirmed,
        correction_end_score=correction_end_score,
        wave_phase=wave_phase,
        wave_confidence=wave_confidence,
    )


def _make_df():
    return pd.DataFrame(
        {
            "close": [100.00, 100.18, 100.42, 100.66],
            "high": [100.15, 100.28, 100.50, 100.72],
            "low": [99.92, 100.05, 100.22, 100.48],
        }
    )


def _supportive_snapshot():
    return {
        "features": {
            "delta_proxy": 0.18,
            "depth_refill_shift": 0.08,
            "depth_imbalance": 0.05,
            "bar_volume_proxy": 0.44,
            "tick_up_ratio": 0.64,
            "rejection_ratio": 0.16,
            "spread_expansion": 1.05,
            "mid_drift_pct": 0.011,
            "spots_count": 12,
            "depth_count": 48,
        }
    }


def _adverse_snapshot():
    return {
        "features": {
            "delta_proxy": -0.03,
            "depth_refill_shift": -0.04,
            "depth_imbalance": -0.02,
            "bar_volume_proxy": 0.16,
            "tick_up_ratio": 0.47,
            "rejection_ratio": 0.42,
            "spread_expansion": 1.27,
            "mid_drift_pct": -0.006,
            "spots_count": 11,
            "depth_count": 46,
        }
    }


def test_fib_quality_gate_accepts_supportive_reversal_template():
    scanner = FiboAdvanceScanner()
    sharpness = {"sharpness_score": 62, "sharpness_band": "sharp"}

    ok, reason, details = scanner._fib_entry_quality_gate(
        "long",
        _make_fibo_ctx(),
        _supportive_snapshot(),
        _make_df(),
        1.0,
        mode="sniper",
        sharpness=sharpness,
    )

    assert ok is True
    assert "fib_quality_ok" in reason
    assert details["reversal_template_applied"] is True
    assert details["reversal_template_score"] >= details["reversal_template_min_score"]


def test_fib_quality_gate_blocks_weak_reversal_template():
    scanner = FiboAdvanceScanner()
    sharpness = {"sharpness_score": 58, "sharpness_band": "normal"}

    ok, reason, details = scanner._fib_entry_quality_gate(
        "long",
        _make_fibo_ctx(),
        _adverse_snapshot(),
        _make_df(),
        1.0,
        mode="sniper",
        sharpness=sharpness,
    )

    assert ok is False
    assert "reversal_template:" in reason
    assert details["reversal_template_applied"] is True


def test_fib_quality_gate_can_require_capture_when_strict(monkeypatch):
    monkeypatch.setattr(fibo_module.config, "FIBO_REVERSAL_TEMPLATE_STRICT_REQUIRE_CAPTURE", True, raising=False)
    scanner = FiboAdvanceScanner()
    sharpness = {"sharpness_score": 60, "sharpness_band": "normal"}

    ok, reason, details = scanner._fib_entry_quality_gate(
        "long",
        _make_fibo_ctx(),
        {"features": {}},
        _make_df(),
        1.0,
        mode="sniper",
        sharpness=sharpness,
    )

    assert ok is False
    assert "reversal_template:capture_unavailable" in reason
    assert details["reversal_template_applied"] is False


def test_fib_quality_gate_allows_confirmed_correction_end_outside_golden_pocket(monkeypatch):
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_ALLOW_CONFIRMED_CORRECTION_END_OUTSIDE_GP", True, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_SCORE", 5.5, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_WAVE_CONFIDENCE", 0.55, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_TEMPLATE_SCORE", 4, raising=False)
    scanner = FiboAdvanceScanner()
    sharpness = {"sharpness_score": 62, "sharpness_band": "sharp"}

    ok, reason, details = scanner._fib_entry_quality_gate(
        "long",
        _make_fibo_ctx(
            ratio=0.56,
            depth=0.59,
            in_gp=False,
            correction_end_confirmed=True,
            correction_end_score=6.2,
            wave_phase="impulse_restart",
            wave_confidence=0.72,
        ),
        _supportive_snapshot(),
        _make_df(),
        1.0,
        mode="sniper",
        sharpness=sharpness,
    )

    assert ok is True
    assert "fib_quality_ok" in reason
    assert details["correction_end_override"] is True
    assert details["reversal_template_applied"] is True


def test_fib_quality_gate_blocks_weak_correction_end_override_without_template(monkeypatch):
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_ALLOW_CONFIRMED_CORRECTION_END_OUTSIDE_GP", True, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_SCORE", 5.5, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_WAVE_CONFIDENCE", 0.55, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_TEMPLATE_SCORE", 4, raising=False)
    scanner = FiboAdvanceScanner()
    sharpness = {"sharpness_score": 58, "sharpness_band": "normal"}

    ok, reason, details = scanner._fib_entry_quality_gate(
        "long",
        _make_fibo_ctx(
            ratio=0.56,
            depth=0.59,
            in_gp=False,
            correction_end_confirmed=True,
            correction_end_score=6.2,
            wave_phase="impulse_restart",
            wave_confidence=0.72,
        ),
        _adverse_snapshot(),
        _make_df(),
        1.0,
        mode="sniper",
        sharpness=sharpness,
    )

    assert ok is False
    assert "correction_end_template_weak" in reason
    assert details["correction_end_override"] is False


def test_fib_quality_gate_blocks_too_shallow_correction_end_override(monkeypatch):
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_ALLOW_CONFIRMED_CORRECTION_END_OUTSIDE_GP", True, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_SCORE", 5.5, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_WAVE_CONFIDENCE", 0.55, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_TEMPLATE_SCORE", 4, raising=False)
    monkeypatch.setattr(fibo_module.config, "FIBO_ADVANCE_CORRECTION_END_MIN_ENTRY_RATIO", 0.55, raising=False)
    scanner = FiboAdvanceScanner()
    sharpness = {"sharpness_score": 62, "sharpness_band": "sharp"}

    ok, reason, details = scanner._fib_entry_quality_gate(
        "long",
        _make_fibo_ctx(
            ratio=0.40,
            depth=0.44,
            in_gp=False,
            correction_end_confirmed=True,
            correction_end_score=6.5,
            wave_phase="impulse_restart",
            wave_confidence=0.78,
        ),
        _supportive_snapshot(),
        _make_df(),
        1.0,
        mode="sniper",
        sharpness=sharpness,
    )

    assert ok is False
    assert "pre_golden_level" in reason
    assert details["correction_end_override"] is False
