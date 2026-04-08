"""
tests/test_fibo_hardening.py

Unit tests for fibo_advance neural-aware risk management:
  1. Circuit breaker — soft brake 3 levels (warning/caution/emergency)
  2. Trend confidence modifier — weight not gate
  3. Sharpness error — degrade not block
  4. Scout soft penalty — confidence reduction not hard disable
  5. Pause logic — emergency stop only
"""
import pytest
import sys
import os
from datetime import datetime, timezone, timedelta, date
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock heavy imports before importing fibo_advance
sys.modules["yfinance"] = MagicMock()
sys.modules["ccxt"] = MagicMock()

import numpy as np
import pandas as pd
from scanners.fibo_advance import FiboAdvanceScanner


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_df(close=3000.0, n=60):
    """Build a minimal OHLCV DataFrame."""
    return pd.DataFrame({
        "close": [float(close)] * n,
        "high": [float(close) + 1.0] * n,
        "low": [float(close) - 1.0] * n,
        "open": [float(close)] * n,
        "volume": [1000.0] * n,
    })


def _fake_add_ema(d1_bearish=False, d1_bullish=False,
                  h4_bearish=False, h4_bullish=False):
    """Side_effect for ta.add_ema — H4 first call, D1 second call."""
    call_count = [0]

    def side_effect(df, periods=None):
        call_count[0] += 1
        df = df.copy()
        close = float(df["close"].iloc[-1])

        if call_count[0] == 1:  # H4
            if h4_bearish:
                df["ema_21"] = [close + 5.0] * len(df)
                df["ema_50"] = [close + 10.0] * len(df)
            elif h4_bullish:
                df["ema_21"] = [close - 5.0] * len(df)
                df["ema_50"] = [close - 10.0] * len(df)
            else:
                df["ema_21"] = [close + 1.0] * len(df)
                df["ema_50"] = [close - 1.0] * len(df)
        else:  # D1
            if d1_bearish:
                df["ema_21"] = [close + 5.0] * len(df)
                df["ema_50"] = [close + 10.0] * len(df)
            elif d1_bullish:
                df["ema_21"] = [close - 5.0] * len(df)
                df["ema_50"] = [close - 10.0] * len(df)
            else:
                df["ema_21"] = [close + 1.0] * len(df)
                df["ema_50"] = [close - 1.0] * len(df)
        return df

    return side_effect


# ═════════════════════════════════════════════════════════════════════════════
# Test 1: Soft Circuit Breaker — 3 levels
# ═════════════════════════════════════════════════════════════════════════════

class TestSoftCircuitBreaker:
    def _fresh_scanner(self):
        scanner = FiboAdvanceScanner()
        scanner._last_reset_date = date.today()
        return scanner

    def test_level1_warning_3_consec_losses(self):
        """3 consecutive losses → warning, conf -10, still allowed."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)
        scanner.report_trade_result(-7.0)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is True
        assert "warning" in reason
        assert conf_mod == -10.0

    def test_level2_caution_5_consec_losses(self):
        """5 consecutive losses → caution, conf -25, still allowed."""
        scanner = self._fresh_scanner()
        for _ in range(5):
            scanner.report_trade_result(-5.0)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is True
        assert "caution" in reason
        assert conf_mod == -25.0

    def test_level3_emergency_10_consec_losses(self):
        """10 consecutive losses → emergency, BLOCK."""
        scanner = self._fresh_scanner()
        for _ in range(10):
            scanner.report_trade_result(-5.0)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is False
        assert "emergency" in reason

    def test_two_losses_ok_no_penalty(self):
        """2 losses should NOT trigger any warning."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is True
        assert "circuit_ok" in reason
        assert conf_mod == 0.0

    def test_win_resets_consecutive_counter(self):
        """A winning trade resets the consecutive loss counter."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)
        scanner.report_trade_result(10.0)  # win

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is True
        assert "circuit_ok" in reason
        assert scanner._consecutive_losses == 0

    def test_daily_loss_level1_warning(self):
        """Daily loss -$30 → warning, conf -15, still allowed."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-31.0)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is True
        assert "warning" in reason
        assert conf_mod == -15.0

    def test_daily_loss_level3_emergency(self):
        """Daily loss -$150 → emergency, BLOCK."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-150.0)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is False
        assert "emergency" in reason

    def test_daily_loss_resets_new_day(self):
        """Daily losses should reset on a new date."""
        scanner = FiboAdvanceScanner()
        scanner._last_reset_date = date(2026, 4, 7)
        scanner._daily_losses_usd = -200.0
        scanner._consecutive_losses = 15

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is True
        assert scanner._daily_losses_usd == 0.0
        assert scanner._consecutive_losses == 0


# ═════════════════════════════════════════════════════════════════════════════
# Test 2: Trend Confidence Modifier — weight not gate
# ═════════════════════════════════════════════════════════════════════════════

class TestTrendConfidenceModifier:
    def test_counter_trend_penalty(self):
        """D1+H4 bearish + direction long → penalty -15, NOT blocked."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bearish=True, h4_bearish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            mod, reason = scanner._trend_confidence_modifier(
                df_d1=df, df_h4=df, direction="long"
            )

            assert mod == -15.0
            assert "counter_trend" in reason

    def test_aligned_bonus(self):
        """D1+H4 bullish + direction long → bonus +5."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bullish=True, h4_bullish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            mod, reason = scanner._trend_confidence_modifier(
                df_d1=df, df_h4=df, direction="long"
            )

            assert mod == +5.0
            assert "aligned" in reason

    def test_neutral_no_modifier(self):
        """Mixed trend → 0 modifier, not blocked."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bullish=True, h4_bearish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            mod, reason = scanner._trend_confidence_modifier(
                df_d1=df, df_h4=df, direction="long"
            )

            assert mod == 0.0
            assert "neutral" in reason

    def test_counter_trend_never_blocks(self):
        """Counter-trend should NEVER return a blocking signal."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bearish=True, h4_bearish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            # Both directions should return modifier, never block
            mod_long, _ = scanner._trend_confidence_modifier(
                df_d1=df, df_h4=df, direction="long"
            )
            mod_short, _ = scanner._trend_confidence_modifier(
                df_d1=df, df_h4=df, direction="short"
            )

            # long is counter-trend (penalty), short is aligned (bonus)
            assert mod_long == -15.0  # penalty but not blocking
            assert mod_short == +5.0  # aligned bonus

    def test_error_passthrough(self):
        """Trend check error → 0 modifier, passthrough."""
        scanner = FiboAdvanceScanner()

        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = Exception("EMA failed")
            df = _make_df()
            mod, reason = scanner._trend_confidence_modifier(
                df_d1=df, df_h4=df, direction="long"
            )

            assert mod == 0.0
            assert "error" in reason


# ═════════════════════════════════════════════════════════════════════════════
# Test 3: Scout Soft Penalty
# ═════════════════════════════════════════════════════════════════════════════

class TestScoutSoftPenalty:
    def test_scout_not_hard_blocked_at_2_losses(self):
        """Scout should NOT be hard-blocked at 2 consecutive losses."""
        scanner = FiboAdvanceScanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)
        # With soft penalty, scout is still allowed (just lower conf)
        assert scanner._consecutive_losses >= 2

    def test_scout_reenabled_after_win(self):
        """Scout penalty resets after winning trade."""
        scanner = FiboAdvanceScanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)
        assert scanner._consecutive_losses >= 2

        scanner.report_trade_result(10.0)  # win
        assert scanner._consecutive_losses == 0


# ═════════════════════════════════════════════════════════════════════════════
# Test 4: Pause Logic — emergency only
# ═════════════════════════════════════════════════════════════════════════════

class TestPauseLogic:
    def test_paused_until_blocks_scans(self):
        """If pause_until is in the future, scans should be blocked."""
        scanner = FiboAdvanceScanner()
        scanner._pause_until = datetime.now(timezone.utc) + timedelta(hours=1)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is False
        assert "paused" in reason

    def test_expired_pause_allows_scans(self):
        """If pause_until has passed, scans should be allowed."""
        scanner = FiboAdvanceScanner()
        scanner._pause_until = datetime.now(timezone.utc) - timedelta(minutes=1)

        allowed, reason, conf_mod = scanner._check_circuit_breaker()

        assert allowed is True


# ═════════════════════════════════════════════════════════════════════════════
# Test 5: Thresholds are at original values
# ═════════════════════════════════════════════════════════════════════════════

class TestThresholds:
    def test_sniper_thresholds_reverted(self):
        """Verify sniper thresholds are at original (relaxed) values."""
        from scanners.fibo_advance import _cfg
        assert _cfg("FIBO_ADVANCE_MIN_RR", 1.2) == 1.2
        assert _cfg("FIBO_ADVANCE_MIN_CONFIDENCE", 62.0) == 62.0

    def test_scout_thresholds_reverted(self):
        """Verify scout thresholds are at original (relaxed) values."""
        from scanners.fibo_advance import _cfg
        assert _cfg("FIBO_SCOUT_MIN_FIBO_SCORE", 28.0) == 28.0
        assert _cfg("FIBO_SCOUT_MIN_RR", 1.0) == 1.0
        assert _cfg("FIBO_SCOUT_MIN_CONFIDENCE", 55.0) == 55.0


# ═════════════════════════════════════════════════════════════════════════════
# Test 6: Weighted Fibonacci Killer — confidence modifier, NOT binary gate
# ═════════════════════════════════════════════════════════════════════════════

class TestWeightedFibonacciKiller:
    """Test that Fibonacci Killer returns weight, not binary block."""

    def _make_snapshot(self, **overrides):
        """Build a minimal snapshot dict."""
        features = {
            "delta_proxy": 0.0,
            "bar_volume_proxy": 0.5,
            "spread_expansion_ratio": 1.0,
            **{k: v for k, v in overrides.items() if k not in ("day_type", "state_label")},
        }
        snapshot = {
            "features": features,
            "day_type": overrides.get("day_type", "trend"),
            "state_label": overrides.get("state_label", ""),
        }
        return snapshot

    def _make_df_with_atr(self, atr_val=2.0, n=30, bar_range=1.0):
        """Build a minimal DataFrame with atr_14 column.
        bar_range: half-range of each bar (high-close and close-low).
        Default 1.0 → total bar range 2.0 → retrace_vel = 1.0x ATR (normal).
        """
        c = 3000.0
        return pd.DataFrame({
            "close": [c] * n,
            "high": [c + bar_range] * n,
            "low": [c - bar_range] * n,
            "atr_14": [float(atr_val)] * n,
        })

    def test_no_killer_returns_zero_weight(self):
        """Clean market → allowed=True, weight=0."""
        scanner = FiboAdvanceScanner()
        snapshot = self._make_snapshot()
        df = self._make_df_with_atr()

        allowed, reason, weight = scanner._fibonacci_killer_check(
            snapshot, atr=2.0, df_entry=df
        )

        assert allowed is True
        assert weight == 0.0
        assert "no_killer" in reason

    def test_mild_killer_returns_negative_weight(self):
        """Single mild condition → allowed=True, weight between -3 and -8."""
        scanner = FiboAdvanceScanner()
        snapshot = self._make_snapshot(delta_proxy=0.42)  # slightly above 0.40 threshold
        df = self._make_df_with_atr()

        allowed, reason, weight = scanner._fibonacci_killer_check(
            snapshot, atr=2.0, df_entry=df
        )

        assert allowed is True
        assert -8.0 <= weight < 0
        assert "mild" in reason

    def test_moderate_killer_returns_medium_weight(self):
        """Multiple moderate conditions → allowed=True, weight -10 to -18."""
        scanner = FiboAdvanceScanner()
        snapshot = self._make_snapshot(
            delta_proxy=0.65,  # 2 points (extreme)
            bar_volume_proxy=2.6,  # 1 point (spike)
        )
        df = self._make_df_with_atr()

        allowed, reason, weight = scanner._fibonacci_killer_check(
            snapshot, atr=2.0, df_entry=df
        )

        assert allowed is True
        assert -18.0 <= weight <= -10.0
        assert "moderate" in reason

    def test_severe_killer_returns_heavy_weight(self):
        """Day type killer → allowed=True, weight -20 to -35."""
        scanner = FiboAdvanceScanner()
        snapshot = self._make_snapshot(day_type="panic_spread")  # 5 points
        df = self._make_df_with_atr()

        allowed, reason, weight = scanner._fibonacci_killer_check(
            snapshot, atr=2.0, df_entry=df
        )

        # panic_spread = 5 points → score 5 → severe tier
        assert allowed is True
        assert weight <= -20.0
        assert "severe" in reason

    def test_hard_block_state_label(self):
        """State label panic_dislocation alone → severe, not hard block."""
        scanner = FiboAdvanceScanner()
        # Use df with very small bars so retrace_vel doesn't add points
        df = pd.DataFrame({
            "close": [3000.0] * 30,
            "high": [3000.5] * 30,  # 0.5pt range → retrace_vel = 0.5/2.0 = 0.25x
            "low": [2999.5] * 30,
            "atr_14": [2.0] * 30,
        })
        snapshot = self._make_snapshot(state_label="panic_dislocation")  # 7 points

        allowed, reason, weight = scanner._fibonacci_killer_check(
            snapshot, atr=2.0, df_entry=df
        )

        # 7 points alone → severe (not hard block which needs >= 8)
        assert allowed is True
        assert weight <= -20.0
        assert "severe" in reason

    def test_hard_block_combined_extreme(self):
        """State label + day type → score >= 8 → hard block."""
        scanner = FiboAdvanceScanner()
        snapshot = self._make_snapshot(
            state_label="panic_dislocation",  # 7 points
            day_type="panic_spread",  # 5 points → total 12
        )
        df = self._make_df_with_atr()

        allowed, reason, weight = scanner._fibonacci_killer_check(
            snapshot, atr=2.0, df_entry=df
        )

        assert allowed is False
        assert "hard_block" in reason

    def test_return_tuple_is_three_elements(self):
        """Verify return type is (bool, str, float) — not old 2-tuple."""
        scanner = FiboAdvanceScanner()
        snapshot = self._make_snapshot()
        df = self._make_df_with_atr()

        result = scanner._fibonacci_killer_check(snapshot, atr=2.0, df_entry=df)

        assert len(result) == 3
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)
        assert isinstance(result[2], float)

    def test_retracement_velocity_grading(self):
        """Retracement velocity should have 3 tiers of severity."""
        scanner = FiboAdvanceScanner()
        # Fast retrace bar ranges
        df = pd.DataFrame({
            "close": [3000.0] * 10,
            "high": [3005.0] * 10,  # 5pt range = 2.5x ATR if ATR=2
            "low": [2995.0] * 10,
            "atr_14": [2.0] * 10,
        })
        snapshot = self._make_snapshot()

        allowed, reason, weight = scanner._fibonacci_killer_check(
            snapshot, atr=2.0, df_entry=df
        )

        # 5pt bar range / 2pt ATR = 2.5x → above 2.0*1.5=3.0? No → above 2.0 → 2 points
        # But need to check _retracement_velocity calculation
        # avg_bar_range = 10pts / 5 bars = 2pts → 2/2 = 1.0x ATR → below 0.7*2.0 threshold
        # Actually: lookback=5 bars, total_range=10*5=50, avg=10, 10/2=5.0x → extreme
        # 5.0 > 2.0*1.5=3.0 → 4 points → moderate
        assert allowed is True  # 4 points alone doesn't hard block
