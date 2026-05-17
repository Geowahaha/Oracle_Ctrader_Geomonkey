import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.modules["yfinance"] = MagicMock()
sys.modules["ccxt"] = MagicMock()

from scanners.fibo_advance import FiboAdvanceScanner
from analysis.fibonacci import FibonacciLevels


def _df():
    return pd.DataFrame(
        {
            "close": [100.0, 100.4, 100.9, 101.2, 101.6],
            "high": [100.2, 100.6, 101.0, 101.4, 101.8],
            "low": [99.8, 100.1, 100.6, 100.9, 101.2],
        }
    )


def _ctx(*, phase="impulse_restart", correction_end_confirmed=True, score=6.4, wave_conf=0.78):
    fib = FibonacciLevels(
        direction="bullish",
        swing_start=95.0,
        swing_end=105.0,
        swing_range=10.0,
        levels={1.0: 95.0, 0.618: 98.82},
        extensions={1.0: 108.0, 1.272: 110.0, 1.618: 113.0, 2.0: 116.0},
        golden_pocket_low=98.82,
        golden_pocket_high=99.5,
        impulse_strength=0.82,
        swing_start_idx=1,
        swing_end_idx=10,
    )
    return SimpleNamespace(
        fib_levels=fib,
        nearest_level_price=98.82,
        nearest_level_ratio=0.618,
        nearest_level_dist_pct=0.02,
        in_golden_pocket=True,
        retracement_depth=0.64,
        retracement_healthy=True,
        elliott_wave_count=2,
        impulse_confirmed=True,
        volume_diminishing=True,
        fibo_confluence_score=72.0,
        reasons=["base_reason"],
        warnings=[],
        wave_phase=phase,
        wave_confidence=wave_conf,
        correction_end_confirmed=correction_end_confirmed,
        correction_end_score=score,
        impulse_birth_confidence=0.0,
        impulse_birth_direction="",
        impulse_age_bars=3,
    )


def test_sniper_wave_phase_drives_confidence_risk_and_promotion():
    scanner = FiboAdvanceScanner()
    signal = scanner._build_signal(
        direction="long",
        fibo_ctx=_ctx(),
        current_price=99.0,
        atr=1.2,
        rsi=35.0,
        session_info={"active_sessions": ["london"]},
        smc_context=None,
        df_entry=_df(),
        vp_adj=0.0,
        vp_reason="",
        sharpness={"sharpness_score": 61, "sharpness_band": "sharp"},
        quality={"reversal_template_applied": True, "reversal_template_reason": "confirmed", "reversal_template_near_level": True, "reversal_template_score": 5, "reversal_template_min_score": 4},
        mode="sniper",
    )

    assert signal is not None
    raw = dict(signal.raw_scores or {})
    assert float(signal.confidence) > 72.0
    assert float(raw.get("ctrader_risk_usd_override") or 0.0) > 1.0
    assert bool(raw.get("fibo_winner_eligible")) is True
    assert str(raw.get("winner_logic_regime") or "") == "strong"
    assert "phase=impulse_restart" in str(signal.tp_reason)
    assert "phase=impulse_restart" in str(signal.sl_reason)


def test_scout_correction_phase_de_risks_trade_profile():
    scanner = FiboAdvanceScanner()
    signal = scanner._scout_scan(
        df_h1=_df(),
        df_m15=_df(),
        current_price=99.0,
        atr_h1=1.2,
        rsi=43.0,
        session_info={"active_sessions": ["london"]},
        snapshot={"features": {"delta_proxy": 0.10, "depth_imbalance": 0.06, "mid_drift_pct": 0.01, "bar_volume_proxy": 0.5, "tick_up_ratio": 0.6, "rejection_ratio": 0.1, "spread_expansion": 1.0, "spots_count": 10, "depth_count": 40}},
        smc_context=None,
        h4_bias="long",
        d1_bias="long",
    )
    # Scan path may return None depending on internal gates; verify phase profile helper directly too.
    profile = scanner._phase_trade_profile(fibo_ctx=_ctx(phase="correction", correction_end_confirmed=False, score=0.0, wave_conf=0.2), mode="scout", anchor_mode="late_retrace")
    assert profile["risk_mult"] < 1.0
    assert profile["conf_bonus"] < 0.0
    if signal is not None:
        assert "phase=" in str(signal.tp_reason)
