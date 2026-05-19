"""Tests for the position_trailing_brain peak-R fix (2026-05-19).

Critical scenario — pid=621968762 incident:
- Short opened with risk 7.89pt
- MFE peaked at ~1.05R (price went from 4549.71 down to 4541.42)
- Then retraced back to entry; r_now dropped to ~0.24
- LEGACY brain saw r_now=0.24 → only lock_r=0.20 → SL barely moved (BE+ε)
- Price reversed up to 4557.69 → original SL hit for -$82

The fix: brain now uses the *peak* r ever observed, NOT current r_now.
Once peak crosses 1R, brain locks 55% even if r_now retraces to 0.5R.
The peak is per-position and only ratchets up.
"""
from __future__ import annotations

from learning.position_trailing_brain import PositionTrailingBrain


def _make_brain(tmp_path) -> PositionTrailingBrain:
    return PositionTrailingBrain(
        db_path=str(tmp_path / "brain.db"),
        model_dir=str(tmp_path / "model"),
    )


def _state(*, pid: int, r_now: float, source: str = "scalp_xauusd") -> dict:
    return {
        "position_id": pid,
        "symbol": "XAUUSD",
        "family": "gold",
        "source_lane": source,
        "r_now": r_now,
        "time_in_trade_minutes": 5.0,
        "vwap_slope_100t": 0.0,
        "tick_velocity": 0.0,
        "depth_imbalance": 0.0,
        "vol_regime_ratio": 1.0,
        "session_overlap_flag": 0.0,
        "active_sl": 0.0,
    }


def test_brain_uses_peak_r_when_current_retraces(tmp_path):
    """The 2026-05-19 incident replay: first tick r_now=1.05 sets peak,
    then a later tick at r_now=0.24 must still return lock based on
    peak, NOT downgrade to the 0.22 step's 0.20 lock."""
    brain = _make_brain(tmp_path)
    # First tick: peak builds up to 1.05
    d1 = brain.get_trailing_decision(_state(pid=621968762, r_now=1.05))
    assert d1.should_move
    # Step 1.0 -> lock 0.55 (mid_lock_55pct)
    assert abs(d1.trail_lock_r - 0.55) < 1e-6
    # Second tick: r_now retraces but peak is locked at 1.05
    d2 = brain.get_trailing_decision(_state(pid=621968762, r_now=0.24))
    # Must still return the 1.0R-tier lock, not downgrade to 0.20.
    assert d2.should_move
    assert d2.trail_lock_r == 0.55


def test_brain_per_position_independence(tmp_path):
    """Peaks are per-position-id. One winning trade's peak must not
    leak into another trade's decision."""
    brain = _make_brain(tmp_path)
    d_winner = brain.get_trailing_decision(_state(pid=1, r_now=2.5))
    assert d_winner.trail_lock_r == 1.30  # 2.0R tier
    # Different position, never had a peak.
    d_new = brain.get_trailing_decision(_state(pid=2, r_now=0.30))
    assert d_new.trail_lock_r == 0.10  # 0.22 micro tier


def test_brain_ratchets_up_when_peak_rises(tmp_path):
    """Climbing peak ratchets the lock; we should see the lock RISE as
    the peak crosses higher thresholds."""
    brain = _make_brain(tmp_path)
    locks = []
    for r in (0.30, 0.55, 1.10, 1.60, 2.10):
        d = brain.get_trailing_decision(_state(pid=42, r_now=r))
        locks.append(d.trail_lock_r)
    # Should monotonically rise: 0.10, 0.25, 0.55, 0.90, 1.30
    assert locks == [0.10, 0.25, 0.55, 0.90, 1.30]


def test_brain_peak_never_falls(tmp_path):
    """After hitting a 3R peak, a retrace to negative r_now must still
    return the 3R-tier lock (not zero)."""
    brain = _make_brain(tmp_path)
    brain.get_trailing_decision(_state(pid=7, r_now=3.20))
    d = brain.get_trailing_decision(_state(pid=7, r_now=-0.40))
    assert d.should_move
    assert d.trail_lock_r == 2.20  # 3.0R tier


def test_brain_below_lowest_threshold_holds(tmp_path):
    """Peak under the lowest threshold (0.22R) means no trail yet."""
    brain = _make_brain(tmp_path)
    d = brain.get_trailing_decision(_state(pid=99, r_now=0.10))
    assert d.should_move is False
    assert d.trail_lock_r == 0.0


def test_brain_passes_canary_lane_flag(tmp_path):
    """is_canary feature should be set correctly from source_lane."""
    brain = _make_brain(tmp_path)
    d = brain.get_trailing_decision(_state(pid=1, r_now=1.10, source="xau_scalp_tick_depth_filter:canary"))
    diags = d.diagnostics
    assert diags["features"]["is_canary"] == 1.0


def test_brain_steps_csv_override(tmp_path, monkeypatch):
    """Operator override via TRAILING_BRAIN_PEAK_STEPS_CSV must replace
    the in-code defaults."""
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "TRAILING_BRAIN_PEAK_STEPS_CSV",
                        "2.0:1.50:tier2,1.0:0.70:tier1,0.5:0.35:tier05")
    brain = PositionTrailingBrain(
        db_path=str(tmp_path / "brain.db"),
        model_dir=str(tmp_path / "model"),
    )
    d_high = brain.get_trailing_decision(_state(pid=1, r_now=2.5))
    assert d_high.trail_lock_r == 1.50
    d_mid = brain.get_trailing_decision(_state(pid=2, r_now=1.0))
    assert d_mid.trail_lock_r == 0.70
    d_low = brain.get_trailing_decision(_state(pid=3, r_now=0.5))
    assert d_low.trail_lock_r == 0.35


def test_brain_decision_log_includes_peak(tmp_path, caplog):
    """The TRAIL DECISION log should include both r_now and peak_r so
    operators can verify peak tracking."""
    import logging
    caplog.set_level(logging.INFO)
    brain = _make_brain(tmp_path)
    brain.get_trailing_decision(_state(pid=1, r_now=1.50))
    assert any("peak_r=1.50" in rec.message for rec in caplog.records)
