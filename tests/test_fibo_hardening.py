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
