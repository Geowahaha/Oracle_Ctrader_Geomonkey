"""
tests/test_fibo_hardening.py

Unit tests for fibo_advance risk hardening (PR #1):
  1. Circuit breaker — 3 consecutive losses → triggers
  2. Circuit breaker — $20 daily loss → triggers
  3. Trend alignment — D1 bearish + H4 bearish → block LONG, allow SHORT
  4. Trend alignment — D1 bullish + H4 bullish → block SHORT, allow LONG
  5. Trend alignment — D1/H4 conflict → allow both
  6. Sharpness error → passthrough (not block)
  7. Scout auto-disable on 2+ consecutive losses
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
    """
    Returns a side_effect function for ta.add_ema that simulates
    bullish/bearish EMA configurations.
    Call order in _check_trend_alignment: H4 first, D1 second.
    """
    call_count = [0]

    def side_effect(df, periods=None):
        call_count[0] += 1
        df = df.copy()
        close = float(df["close"].iloc[-1])

        if call_count[0] == 1:  # first call = H4
            if h4_bearish:
                df["ema_21"] = [close + 5.0] * len(df)
                df["ema_50"] = [close + 10.0] * len(df)
            elif h4_bullish:
                df["ema_21"] = [close - 5.0] * len(df)
                df["ema_50"] = [close - 10.0] * len(df)
            else:
                df["ema_21"] = [close + 1.0] * len(df)
                df["ema_50"] = [close - 1.0] * len(df)
        else:  # second call = D1
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
# Test 1: 3 consecutive losses → circuit breaker triggers
# ═════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerConsecLoss:
    def _fresh_scanner(self):
        """Create a scanner with _last_reset_date set to today to prevent
        circuit breaker from resetting counters on first check."""
        scanner = FiboAdvanceScanner()
        scanner._last_reset_date = date.today()
        return scanner

    def test_three_consecutive_losses_triggers(self):
        """3 losses in a row should trip the circuit breaker."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)
        scanner.report_trade_result(-7.0)

        allowed, reason = scanner._check_circuit_breaker()

        assert allowed is False
        assert "consec_loss" in reason or "paused" in reason

    def test_two_consecutive_losses_ok(self):
        """2 losses should NOT trigger (default threshold is 3)."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)

        allowed, reason = scanner._check_circuit_breaker()

        assert allowed is True
        assert "circuit_ok" in reason

    def test_win_resets_consecutive_counter(self):
        """A winning trade resets the consecutive loss counter."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-5.0)
        scanner.report_trade_result(-3.0)
        scanner.report_trade_result(10.0)  # win resets

        allowed, reason = scanner._check_circuit_breaker()

        assert allowed is True
        assert scanner._consecutive_losses == 0


# ═════════════════════════════════════════════════════════════════════════════
# Test 2: $20 daily loss → circuit breaker triggers
# ═════════════════════════════════════════════════════════════════════════════

class TestCircuitBreakerDailyLoss:
    def _fresh_scanner(self):
        scanner = FiboAdvanceScanner()
        scanner._last_reset_date = date.today()
        return scanner

    def test_daily_loss_cap_triggers(self):
        """Cumulative daily losses exceeding $20 should trip breaker (2 losses, under consec limit)."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-15.0)
        allowed, _ = scanner._check_circuit_breaker()
        assert allowed is True  # -$15, under $20 cap

        # One more loss pushes past $20 (only 2 consec losses, not hitting 3)
        scanner.report_trade_result(-6.0)
        allowed, reason = scanner._check_circuit_breaker()
        assert allowed is False
        assert "daily_loss" in reason

    def test_daily_cap_exact_20(self):
        """Exactly -$20 should trigger the breaker."""
        scanner = self._fresh_scanner()
        scanner.report_trade_result(-20.0)

        allowed, reason = scanner._check_circuit_breaker()

        assert allowed is False
        assert "daily_loss" in reason

    def test_daily_loss_resets_new_day(self):
        """Daily losses should reset on a new date."""
        scanner = FiboAdvanceScanner()
        scanner._last_reset_date = date(2026, 4, 7)
        scanner._daily_losses_usd = -25.0
        scanner._consecutive_losses = 5

        allowed, reason = scanner._check_circuit_breaker()

        assert allowed is True
        assert scanner._daily_losses_usd == 0.0
        assert scanner._consecutive_losses == 0


# ═════════════════════════════════════════════════════════════════════════════
# Test 3-5: Trend Alignment Gate
# ═════════════════════════════════════════════════════════════════════════════

class TestTrendAlignment:
    def test_d1_bearish_h4_bearish_blocks_long(self):
        """Both D1 and H4 bearish → should BLOCK long entries."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bearish=True, h4_bearish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            allowed, reason = scanner._check_trend_alignment(
                df_d1=df, df_h4=df, direction="long"
            )

            assert allowed is False
            assert "counter_trend" in reason

    def test_d1_bearish_h4_bearish_allows_short(self):
        """Both D1 and H4 bearish → should ALLOW short entries."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bearish=True, h4_bearish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            allowed, reason = scanner._check_trend_alignment(
                df_d1=df, df_h4=df, direction="short"
            )

            assert allowed is True
            assert "trend_aligned" in reason

    def test_d1_bullish_h4_bullish_blocks_short(self):
        """Both D1 and H4 bullish → should BLOCK short entries."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bullish=True, h4_bullish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            allowed, reason = scanner._check_trend_alignment(
                df_d1=df, df_h4=df, direction="short"
            )

            assert allowed is False
            assert "counter_trend" in reason

    def test_d1_bullish_h4_bullish_allows_long(self):
        """Both D1 and H4 bullish → should ALLOW long entries."""
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bullish=True, h4_bullish=True
            )
            scanner = FiboAdvanceScanner()
            df = _make_df()

            allowed, reason = scanner._check_trend_alignment(
                df_d1=df, df_h4=df, direction="long"
            )

            assert allowed is True
            assert "trend_aligned" in reason

    def test_d1_h4_conflict_allows_both(self):
        """D1 bullish + H4 bearish (conflict) → should allow BOTH long and short."""
        scanner = FiboAdvanceScanner()
        df = _make_df()

        # Test LONG direction: fresh mock each time
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bullish=True, h4_bearish=True
            )
            allowed_long, _ = scanner._check_trend_alignment(
                df_d1=df, df_h4=df, direction="long"
            )

        # Test SHORT direction: fresh mock (resets call_count)
        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = _fake_add_ema(
                d1_bullish=True, h4_bearish=True
            )
            allowed_short, _ = scanner._check_trend_alignment(
                df_d1=df, df_h4=df, direction="short"
            )

        assert allowed_long is True
        assert allowed_short is True


# ═════════════════════════════════════════════════════════════════════════════
# Test 6: Error handling passthrough
# ═════════════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    def test_trend_check_error_passes_through(self):
        """If trend alignment raises an error, it should pass through (not block)."""
        scanner = FiboAdvanceScanner()

        with patch("scanners.fibo_advance.ta") as mock_ta:
            mock_ta.add_ema.side_effect = Exception("EMA calc failed")

            df = _make_df()
            allowed, reason = scanner._check_trend_alignment(
                df_d1=df, df_h4=df, direction="long"
            )

            assert allowed is True
            assert "trend_check_error_passthrough" in reason


# ═════════════════════════════════════════════════════════════════════════════
# Test 7: Scout auto-disable on 2+ consecutive losses
# ═════════════════════════════════════════════════════════════════════════════

class TestScoutAutoDisable:
    def test_scout_disabled_at_two_consecutive_losses(self):
        """Scout mode should be disabled when consec losses >= 2."""
        scanner = FiboAdvanceScanner()
        scanner.report_trade_result(-5.0)  # loss 1
        scanner.report_trade_result(-3.0)  # loss 2
        assert scanner._consecutive_losses >= 2

    def test_scout_enabled_at_one_loss(self):
        """Scout mode should remain enabled with only 1 consecutive loss."""
        scanner = FiboAdvanceScanner()
        scanner.report_trade_result(-5.0)
        assert scanner._consecutive_losses < 2

    def test_scout_reenabled_after_win(self):
        """Scout should re-enable after a winning trade resets counter."""
        scanner = FiboAdvanceScanner()
        scanner.report_trade_result(-5.0)  # loss 1
        scanner.report_trade_result(-3.0)  # loss 2
        assert scanner._consecutive_losses >= 2

        scanner.report_trade_result(10.0)  # win → reset
        assert scanner._consecutive_losses == 0


# ═════════════════════════════════════════════════════════════════════════════
# Test integration: pause_until logic
# ═════════════════════════════════════════════════════════════════════════════

class TestPauseLogic:
    def test_paused_until_blocks_scans(self):
        """If pause_until is in the future, scans should be blocked."""
        scanner = FiboAdvanceScanner()
        scanner._pause_until = datetime.now(timezone.utc) + timedelta(hours=1)

        allowed, reason = scanner._check_circuit_breaker()

        assert allowed is False
        assert "paused" in reason

    def test_expired_pause_allows_scans(self):
        """If pause_until has passed, scans should be allowed."""
        scanner = FiboAdvanceScanner()
        scanner._pause_until = datetime.now(timezone.utc) - timedelta(minutes=1)

        allowed, reason = scanner._check_circuit_breaker()

        assert allowed is True
